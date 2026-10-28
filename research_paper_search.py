"""
Research Paper PDF Search Tool
==============================
Indexes and searches academic PDFs by content (title, abstract, keywords,
authors, body). A per-folder SQLite database (FTS5 when available) caches
results so only new or modified files are re-indexed on each run.

Quick start:
    pip install -r requirements.txt
    python research_paper_search.py              # launch the desktop UI
    python research_paper_search.py --index DIR  # build the index headlessly

Configuration is read from environment variables (see README.md); logs are
written to stderr. Nothing operational is hard-coded into the source.
"""

import argparse
import datetime
import functools
import hashlib
import logging
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, NamedTuple, Optional

# PDF text backends, fastest first: pdfium (C++, Chrome's PDF engine) is the
# primary reader; pdfplumber and pypdf (pure Python) are fallbacks.
try:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    PDFIUM_OK = True
except ImportError:
    PDFIUM_OK = False

try:
    import pdfplumber

    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

# Optional: rapidfuzz for fast fuzzy scoring (difflib fallback).

try:
    from rapidfuzz import fuzz

    RAPIDFUZZ_OK = True
except ImportError:
    RAPIDFUZZ_OK = False
    import difflib


LOGGER = logging.getLogger("research_paper_search")


def configure_logging() -> None:
    """Emit logs to stderr as an event stream (12-factor XI)."""
    level_name = os.environ.get("RPS_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # pdfminer (via pdfplumber) emits noisy WARNINGs for malformed font/box
    # descriptors ("Could not get FontBBox ...") that are harmless to indexing.
    # Keep real errors, drop the noise.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


configure_logging()


# -----------------------------------------------------------------------------
# CONFIGURATION (12-factor III: read from the environment, with sane defaults)
# -----------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default


def _default_index_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "research_paper_search", "indexes")


# Indexes live in a local cache directory, not inside the scanned folder: that
# folder may be read-only, and SQLite's WAL mode does not work on network shares.
INDEX_DIR = os.environ.get("RPS_INDEX_DIR") or _default_index_dir()
MAX_FRONT_PAGES = _env_int("RPS_MAX_FRONT_PAGES", 4)
ABSTRACT_CHARS = _env_int("RPS_ABSTRACT_CHARS", 1200)
# The first FULLTEXT_CHARS characters of each paper go into the FTS index for
# search; the rest is stored compressed so exact collection can check the whole
# text without re-reading the PDF. 0 means "index everything".
FULLTEXT_CHARS = _env_int("RPS_FULLTEXT_CHARS", 200000)
# Safety cap on text read from one PDF (0 = none). Longer documents are marked
# truncated and re-read in full only when Collect has to check them.
MAX_TEXT_CHARS = _env_int("RPS_MAX_TEXT_CHARS", 5000000)
SEARCH_CANDIDATE_LIMIT = _env_int("RPS_SEARCH_CANDIDATE_LIMIT", 240)
INDEX_COMMIT_BATCH = _env_int("RPS_INDEX_COMMIT_BATCH", 20)
SQLITE_BUSY_MS = _env_int("RPS_SQLITE_BUSY_MS", 5000)
INDEX_WORKERS = _env_int("RPS_INDEX_WORKERS", 0)  # 0 = auto (detect CPU count)
# Result cards built per event-loop tick; keeps the search box responsive.
RENDER_BATCH_SIZE = _env_int("RPS_RENDER_BATCH", 4)
# Only surface genuine research papers / institutional reports; discard the rest.
REQUIRE_SCHOLARLY = os.environ.get("RPS_REQUIRE_SCHOLARLY", "1") not in ("0", "false", "False", "")
# Score a document needs to count as a paper/report (strong signals such as a
# DOI or a references section score 2, supporting signals score 1).
SCHOLARLY_MIN_SCORE = _env_int("RPS_SCHOLARLY_MIN_SCORE", 2)
# Below this much extracted text a PDF is treated as having no text layer
# (scanned image or damaged file) rather than judged as a non-paper.
MIN_TEXT_CHARS = 200

# Weights for the Python re-ranking pass. It runs only over the small,
# high-signal fields loaded per candidate; the full body is ranked by BM25 in
# SQLite, not here, so it must not appear in this map.
SCORE_WEIGHTS = {
    "title": 5.0,
    "keywords": 4.0,
    "abstract": 3.0,
    "authors": 2.0,
}

# Columns pulled into Python per candidate. `fulltext` is deliberately excluded
# so large bodies never enter the hot search path (BM25 reads it in SQLite).
RESULT_COLUMNS = (
    "id", "path", "filename", "title", "authors", "year",
    "abstract", "keywords", "doi", "arxiv_id",
)
_ROW_SELECT = ", ".join(RESULT_COLUMNS)

# Fields the SQL fallback (no FTS5) substring-scans — includes the full body so
# the degraded path can still find body matches.
SEARCH_FIELDS = ("title", "keywords", "abstract", "authors", "fulltext")

ARXIV_RE = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


# -----------------------------------------------------------------------------
# PDF TEXT EXTRACTION
# -----------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace and trim leading or trailing junk."""
    return re.sub(r"\s+", " ", text).strip()


# Hyphen variants that should compare equal to "-"; the soft hyphen is invisible
# in print and is simply dropped.
_HYPHEN_TRANSLATION = str.maketrans({"‐": "-", "‑": "-", "­": None})


def normalize_text(text: str) -> str:
    """
    Canonicalise text so it compares the way it reads. NFKC expands typographic
    ligatures that publisher PDFs emit ("classiﬁcation" -> "classification") and
    folds other compatibility characters; hyphen variants become "-". Applied
    to extracted text, search terms, and collection keywords alike.
    """
    return unicodedata.normalize("NFKC", text).translate(_HYPHEN_TRANSLATION)


_LINE_BREAK_HYPHEN_RE = re.compile(r"(\w+)-[ \t]*\r?\n[ \t]*(\w+)")
_WORD_RE = re.compile(r"\w+")


def _dehyphenate(text: str, vocabulary: set[str]) -> str:
    """
    Repair words split across a line break ("learn-\\ning"). If the joined form
    occurs elsewhere in the same document it is a typesetting break and the
    pieces are fused ("learning"); otherwise the hyphen is genuine and kept
    ("self-supervised"), so neither form is corrupted.
    """
    def join(match: re.Match) -> str:
        head, tail = match.group(1), match.group(2)
        if (head + tail).lower() in vocabulary:
            return head + tail
        return f"{head}-{tail}"

    return _LINE_BREAK_HYPHEN_RE.sub(join, text)


def _accumulate_pages(
    page_texts,
    max_front_pages: int,
    char_limit: int,
) -> tuple[list[str], str, bool]:
    """
    Consume per-page text, returning the first ``max_front_pages`` pages (for
    field extraction) and the concatenated body up to ``char_limit`` chars
    (``char_limit <= 0`` means the whole document). All text is normalised and
    line-break hyphenation is repaired, so search and exact matching see words
    as they read rather than as the PDF encodes them.
    """
    front_pages: list[str] = []
    text_parts: list[str] = []
    collected = 0
    unlimited = char_limit <= 0

    for page_index, text in enumerate(page_texts):
        # Unify line endings, and turn pdfium's soft-hyphen line-break markers
        # back into "-\n" so _dehyphenate decides how to rejoin the word.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = normalize_text(text.replace("￾", "-\n").replace("\x02", "-\n"))
        want_more = unlimited or collected < char_limit
        if page_index >= max_front_pages and not want_more:
            break
        if page_index < max_front_pages:
            front_pages.append(text)
        if text and want_more:
            chunk = text if unlimited else text[: char_limit - collected]
            text_parts.append(chunk)
            collected += len(chunk)

    # Join pages with a newline so a word hyphenated across a page break is
    # repaired too; whitespace is collapsed later by _clean.
    body = "\n".join(text_parts)
    vocabulary = {word.lower() for word in _WORD_RE.findall(body)}
    for page in front_pages:
        vocabulary.update(word.lower() for word in _WORD_RE.findall(page))

    truncated = not unlimited and collected >= char_limit
    return (
        [_dehyphenate(page, vocabulary) for page in front_pages],
        _dehyphenate(body, vocabulary),
        truncated,
    )


class ExtractedPDF(NamedTuple):
    front_pages: list[str]
    body: str
    meta_title: str = ""
    meta_year: Optional[int] = None
    font_title: str = ""
    truncated: bool = False  # body stopped at char_limit; the PDF has more text


# pdfium is not thread-safe within one process. Worker processes each have their
# own copy, so this only serialises in-process (serial or GUI-thread) extraction.
_PDFIUM_LOCK = threading.Lock()

_TITLE_SKIP_RE = re.compile(
    r"^(https?://|www\.|©|\(c\)|copyright|doi\b|issn|vol\.|volume\b|arxiv|preprint|"
    r"received|accepted|published|journal of|proceedings|transactions on|e-?mail|"
    r"abstract$|keywords?$)",
    re.IGNORECASE,
)
_META_YEAR_RE = re.compile(r"(?<!\d)((?:19[5-9]|20\d)\d)(?!\d)")


def _looks_like_title(text: str) -> bool:
    if not 8 <= len(text) <= 300 or len(text.split()) < 2:
        return False
    if _TITLE_SKIP_RE.match(text):
        return False
    letters = sum(char.isalpha() or char.isspace() for char in text)
    return letters / len(text) > 0.6


def _as_text(value) -> str:
    """Metadata values arrive as str, bytes or PDF objects depending on backend."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-16", "utf-8", "latin-1"):
            with suppress(UnicodeDecodeError):
                return value.decode(encoding)
    return str(value)


def _meta_year(value) -> Optional[int]:
    match = _META_YEAR_RE.search(_as_text(value))
    return int(match.group(1)) if match else None


def _font_size_title(textpage, scan_chars: int = 2500) -> str:
    """
    The title is normally the largest text near the top of page 1. Group the
    first ``scan_chars`` characters into runs of equal font size and return the
    largest run that reads like a title and is clearly bigger than body text.
    Margin stamps (e.g. arXiv's) and running headers fail ``_looks_like_title``.
    """
    count = min(textpage.count_chars(), scan_chars)
    raw_page = textpage.raw
    runs: list[list] = []  # [font size, characters]
    char_counts: dict[float, int] = {}
    for index in range(count):
        char = chr(pdfium_raw.FPDFText_GetUnicode(raw_page, index))
        if char.isspace() or char in "￾\x02":
            if runs:
                runs[-1][1].append(" " if char.isspace() else "-")
            continue
        if not char.isprintable():
            continue
        size = round(pdfium_raw.FPDFText_GetFontSize(raw_page, index), 1)
        char_counts[size] = char_counts.get(size, 0) + 1
        if runs and abs(runs[-1][0] - size) < 0.6:
            runs[-1][1].append(char)
        else:
            runs.append([size, [char]])
    if not char_counts:
        return ""
    body_size = max(char_counts, key=char_counts.get)  # most common size = body text
    best, best_size = "", 0.0
    for size, chars in runs:
        if size < body_size * 1.15 or size <= best_size:
            continue
        text = _clean(normalize_text("".join(chars)))
        if _looks_like_title(text):
            best, best_size = text, size
    return best


def _read_pdfium(pdf_path: str, max_front_pages: int, char_limit: int) -> ExtractedPDF:
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            meta: dict = {}
            with suppress(Exception):
                meta = pdf.get_metadata_dict()
            font_title = ""

            def texts():
                nonlocal font_title
                for index in range(len(pdf)):
                    page = textpage = None
                    try:
                        page = pdf[index]
                        textpage = page.get_textpage()
                        if index == 0:
                            with suppress(Exception):
                                font_title = _font_size_title(textpage)
                        text = textpage.get_text_range()
                    except pdfium.PdfiumError:
                        text = ""  # one bad page must not lose the document
                    finally:
                        if textpage is not None:
                            textpage.close()
                        if page is not None:
                            page.close()
                    yield text

            pages = texts()
            try:
                front, body, truncated = _accumulate_pages(pages, max_front_pages, char_limit)
            finally:
                pages.close()
        finally:
            pdf.close()
    return ExtractedPDF(front, body, _as_text(meta.get("Title")),
                        _meta_year(meta.get("CreationDate")), font_title, truncated)


def _read_pdfplumber(pdf_path: str, max_front_pages: int, char_limit: int) -> ExtractedPDF:
    with pdfplumber.open(pdf_path) as pdf:
        meta = pdf.metadata or {}

        def texts():
            for page in pdf.pages:
                try:
                    yield page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                except Exception:
                    yield ""

        pages = texts()
        try:
            front, body, truncated = _accumulate_pages(pages, max_front_pages, char_limit)
        finally:
            pages.close()
    return ExtractedPDF(front, body, _as_text(meta.get("Title")),
                        _meta_year(meta.get("CreationDate")), truncated=truncated)


def _read_pypdf(pdf_path: str, max_front_pages: int, char_limit: int) -> ExtractedPDF:
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    meta = reader.metadata or {}

    def texts():
        for page in reader.pages:
            try:
                yield page.extract_text() or ""
            except Exception:
                yield ""

    front, body, truncated = _accumulate_pages(texts(), max_front_pages, char_limit)
    return ExtractedPDF(front, body, _as_text(meta.get("/Title")),
                        _meta_year(meta.get("/CreationDate")), truncated=truncated)


def _extract_pdf_content(
    pdf_path: str,
    max_front_pages: int = MAX_FRONT_PAGES,
    char_limit: int = FULLTEXT_CHARS,
) -> ExtractedPDF:
    """
    Extract the front pages, the full body text, and PDF metadata in one pass.
    Backends are tried fastest first (pdfium, then pdfplumber, then pypdf). If a
    backend opens the file but finds (almost) no text, the next one is tried too,
    so a font-encoding quirk in one engine does not cost the document.
    """
    backends = []
    if PDFIUM_OK:
        backends.append(("pdfium", _read_pdfium))
    if PDFPLUMBER_OK:
        backends.append(("pdfplumber", _read_pdfplumber))
    backends.append(("pypdf", _read_pypdf))

    best: Optional[ExtractedPDF] = None
    last_error: Optional[Exception] = None
    for name, read in backends:
        try:
            result = read(pdf_path, max_front_pages, char_limit)
        except Exception as exc:
            last_error = exc
            LOGGER.debug("%s could not read %s", name, pdf_path, exc_info=True)
            continue
        if len(result.body.strip()) >= MIN_TEXT_CHARS:
            return result
        if best is None or len(result.body.strip()) > len(best.body.strip()):
            best = result
    if best is not None:
        return best
    LOGGER.warning("Could not read %s: %s", pdf_path, last_error)
    return ExtractedPDF([], "")


# -----------------------------------------------------------------------------
# FIELD EXTRACTION
# -----------------------------------------------------------------------------

# Publishers, indexes, preprint servers, venues and report issuers, matched as
# whole words only (bare substrings once made "message" count as SAGE and
# "signature" as Nature). Everyday words (nature, sage, frontiers) and generic
# letterhead terms (university, institute, department of) are not evidence.
REPUTABLE_MARKERS = (
    "doi.org", "arxiv", "biorxiv", "medrxiv", "ssrn", "pubmed", "pubmed central",
    "elsevier", "sciencedirect", "springer", "springer nature", "nature publishing group",
    "wiley", "ieee", "acm", "jstor", "taylor & francis", "sage publications",
    "oxford university press", "cambridge university press", "plos", "mdpi", "bmj",
    "aaas", "frontiers in", "cell press", "annual reviews",
    "proceedings of", "journal of", "transactions on", "conference on", "symposium on",
    "technical report", "working paper", "national laboratory",
    "world health organization", "nasa", "nist", "cern", "oecd", "unesco", "world bank",
    "european commission", "national institutes of health", "research council",
    "national academies",
)
# Official issuers of reports and standards, and the report types they publish.
# Named on a document's cover they are strong evidence of an institutional
# report; mentioned anywhere else they only support other evidence. Generic
# "central bank" is not here: it is a topic ("central bank digital currency").
ISSUER_MARKERS = (
    "bank for international settlements", "bis innovation hub",
    "committee on payments and market infrastructures", "financial stability board",
    "international monetary fund", "world bank", "oecd", "european central bank",
    "eurosystem", "european commission", "european banking authority",
    "european securities and markets authority", "federal reserve", "reserve bank",
    "deutsche bundesbank", "bank of england", "bank of canada", "bank of japan",
    "banque de france", "banca d'italia", "banco de españa", "swiss national bank",
    "sveriges riksbank", "monetary authority of singapore", "hong kong monetary authority",
    "people's bank of china", "world health organization", "nasa", "nist", "cern",
    "unesco", "national academies", "national institutes of health",
    # Bare "IETF" is a topic in networking slides; IETF documents identify
    # themselves as an Internet-Draft / RFC (or "draft-ietf-..." below).
    "internet engineering task force", "iso/iec", "w3c",
    # report types
    "consultation paper", "discussion paper", "working paper", "staff working paper",
    "staff analytical paper", "staff discussion paper", "occasional paper", "policy paper",
    "technical report", "economic bulletin", "monthly report", "special publication",
    "internet-draft", "request for comments",
)
_REPUTABLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in (*REPUTABLE_MARKERS, *ISSUER_MARKERS)) + r")\b",
    re.IGNORECASE,
)
_ISSUER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in ISSUER_MARKERS) + r")\b|\bdraft-ietf-[a-z0-9-]+",
    re.IGNORECASE,
)
# How much of page 1 counts as the cover: masthead, title block, issuer line.
COVER_CHARS = 1500
# A heading starts its own line, optionally numbered ("3", "3.1", "III.").
_HEADING_PREFIX = r"^[ \t]*(?:(?:\d+(?:\.\d+)*|[IVX]+)\.?[ \t]+)?"
_REFERENCES_HEADING_RE = re.compile(
    _HEADING_PREFIX + r"(?:references|bibliography|works cited|literature cited)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECTION_HEADING_RE = re.compile(
    _HEADING_PREFIX
    + r"(introduction|background|related work|materials and methods|methods?|methodology|"
    r"results|discussion|conclusions?|executive summary|acknowledge?ments?)\b[^\n]{0,30}$",
    re.IGNORECASE | re.MULTILINE,
)
_NUMERIC_CITATION_RE = re.compile(r"\[\d{1,3}(?:\s*[,–-]\s*\d{1,3})*\]")
_ET_AL_RE = re.compile(r"\bet al\.", re.IGNORECASE)


def _section_key(name: str) -> str:
    name = name.lower()
    if "method" in name:
        return "methods"
    if name.startswith("conclusion"):
        return "conclusions"
    if name.startswith("acknowledg"):
        return "acknowledgements"
    return name


def classify_document(fields: dict, raw_text: Optional[str] = None) -> tuple[bool, str]:
    """
    Decide whether an extracted document is a genuine research paper or report.

    Strong evidence scores 2: a DOI, an arXiv id, a References/Bibliography
    heading, or an official issuer or report type named on the cover ("Bank of
    England ... Discussion Paper", "draft-ietf-..."). Supporting evidence scores
    1: an abstract, two or more standard section headings, in-text citations, a
    reputable publisher/venue/institution named anywhere.
    A document needs ``SCHOLARLY_MIN_SCORE``. ``raw_text`` keeps line breaks, so a
    heading can be told apart from the same word in running prose ("payment
    methods" on an invoice is not a Methods section). Returns
    ``(is_scholarly, reason)``.
    """
    if raw_text is None:
        raw_text = "\n".join(str(fields.get(key, "")) for key in ("title", "abstract", "keywords", "fulltext"))
    score = 0
    signals: list[str] = []

    def add(points: int, label: str) -> None:
        nonlocal score
        score += points
        signals.append(label)

    if fields.get("doi"):
        add(2, "DOI")
    if fields.get("arxiv_id"):
        add(2, "arXiv id")
    if _REFERENCES_HEADING_RE.search(raw_text):
        add(2, "references section")
    issuer = _ISSUER_RE.search(raw_text[:COVER_CHARS])
    if issuer:
        add(2, f"issued by '{issuer.group(0).lower()}'")
    if fields.get("abstract"):
        add(1, "abstract")
    sections = {_section_key(name) for name in _SECTION_HEADING_RE.findall(raw_text)}
    if len(sections) >= 2:
        add(1, "sections: " + ", ".join(sorted(sections)))
    if len(_NUMERIC_CITATION_RE.findall(raw_text)) >= 5 or len(_ET_AL_RE.findall(raw_text)) >= 3:
        add(1, "citations")
    marker = None if issuer else _REPUTABLE_RE.search(raw_text)
    if marker:
        add(1, f"source '{marker.group(0).lower()}'")

    is_scholarly = score >= max(1, SCHOLARLY_MIN_SCORE)
    reason = ", ".join(signals) if signals else "no scholarly markers found"
    return is_scholarly, reason


def _extract_fields(pdf_path: str) -> dict[str, object]:
    """
    Extract structured fields from a research-paper PDF: title, authors, year,
    abstract, keywords, fulltext (the first FULLTEXT_CHARS of the body, for the
    FTS index), tail (the rest of the body, zlib-compressed), doi, arxiv_id, plus
    the flags is_scholarly, has_text, truncated and doc_reason.
    """
    extracted = _extract_pdf_content(
        pdf_path,
        max_front_pages=MAX_FRONT_PAGES,
        char_limit=MAX_TEXT_CHARS,
    )
    if not extracted.front_pages:
        return {"is_scholarly": 0, "has_text": 0, "doc_reason": "unreadable PDF (could not be parsed)"}

    pages, body = extracted.front_pages, extracted.body
    clean_body = _clean(body)

    page0 = pages[0]
    combined_front = "\n".join(pages[:2])

    title = _choose_title(extracted.meta_title, extracted.font_title, page0)
    authors = _guess_authors(page0, title)
    year = _guess_year(page0, combined_front, extracted.meta_year)

    abstract = _extract_block(combined_front, _ABSTRACT_HEAD_RE, ABSTRACT_CHARS)
    keywords = _extract_block(combined_front, _KEYWORDS_HEAD_RE, 400, max_lines=2)

    doi_match = DOI_RE.search(combined_front)
    arxiv_match = ARXIV_RE.search(combined_front)

    # Split exactly, so head + tail reproduces the whole cleaned text.
    split = FULLTEXT_CHARS if FULLTEXT_CHARS > 0 else len(clean_body)
    head, rest = clean_body[:split], clean_body[split:]

    fields: dict[str, object] = {
        "title": _clean(title)[:300],
        "authors": _clean(authors)[:300],
        "year": year,
        "abstract": _clean(abstract)[:ABSTRACT_CHARS],
        "keywords": _clean(keywords)[:400],
        "fulltext": head,
        "tail": zlib.compress(rest.encode("utf-8")) if rest else None,
        # A DOI at the end of a sentence must not keep the full stop.
        "doi": doi_match.group(0).rstrip(".,;:)]}'\"")[:200] if doi_match else "",
        "arxiv_id": arxiv_match.group(1)[:50] if arxiv_match else "",
        "truncated": int(extracted.truncated),
    }
    if len(clean_body) < MIN_TEXT_CHARS:
        # Nothing to judge: report it as unreadable, not as a non-paper.
        fields.update(is_scholarly=0, has_text=0, doc_reason="no text layer (scanned image? needs OCR)")
    else:
        # Judge on the same leading part of the text as before the full text was
        # stored: headings deep inside a whole book are not evidence of a paper.
        judged = body[:FULLTEXT_CHARS] if FULLTEXT_CHARS > 0 else body
        is_scholarly, reason = classify_document(fields, raw_text=judged)
        fields.update(is_scholarly=int(is_scholarly), has_text=1, doc_reason=reason[:200])
    return fields


_BAD_META_TITLE_RE = re.compile(
    r"^(microsoft (word|powerpoint)|untitled|title\b|document\d*\b|slide)|"
    r"\.(pdf|docx?|tex|dvi|ps|rtf|odt|indd|qxd|pptx?)\s*$",
    re.IGNORECASE,
)
_VENUE_NAME_RE = re.compile(r"^(the\s+)?(journal|proceedings|transactions|annals|bulletin|archives)\b", re.IGNORECASE)


def _word_overlap(text: str, other: str) -> float:
    """Fraction of ``text``'s words (3+ letters) that also occur in ``other``."""
    words = [word for word in _WORD_RE.findall(text.lower()) if len(word) >= 3]
    if not words:
        return 0.0
    other_words = set(_WORD_RE.findall(other.lower()))
    return sum(word in other_words for word in words) / len(words)


def _choose_title(meta_title: str, font_title: str, page0: str) -> str:
    """
    Prefer the PDF metadata title when it is plausible and actually printed on
    page 1 (it is exact and complete); otherwise the largest-font text on page 1;
    otherwise the first title-shaped line. When metadata and the large-font text
    disagree, the printed text wins: metadata is sometimes a journal name or a
    leftover file name ("Microsoft Word - draft3.docx").
    """
    meta = _clean(normalize_text(meta_title))
    meta_ok = (
        _looks_like_title(meta)
        and not _BAD_META_TITLE_RE.search(meta)
        and not _VENUE_NAME_RE.match(meta)
        and _word_overlap(meta, page0) >= 0.8
    )
    if font_title:
        return meta if meta_ok and _word_overlap(meta, font_title) >= 0.6 else font_title
    return meta if meta_ok else _guess_title(page0)


# A year token that is not part of a DOI, arXiv id or longer number.
_YEAR = r"(?<![\d./])((?:19[5-9]|20\d)\d)(?![.]?\d)"
# Optional date before the year: "15 March ", "March 15, ", "March ".
_DATE_PREFIX = (
    r"[ \t]*:?[ \t]*(?:on[ \t]+)?(?:\d{1,2}(?:st|nd|rd|th)?[ \t]+[A-Za-z]{3,9}\.?,?[ \t]+"
    r"|[A-Za-z]{3,9}\.?[ \t]+(?:\d{1,2},?[ \t]+)?)?"
)
_ARXIV_STAMP_RE = re.compile(
    r"arXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?\s*\[[^\]\n]{1,40}\]\s*\d{1,2}\s+[A-Za-z]{3,9}\.?\s+" + _YEAR,
    re.IGNORECASE,
)
_ARXIV_ID_DATE_RE = re.compile(r"arXiv:\s*(\d{2})(\d{2})\.\d{4,5}", re.IGNORECASE)
# Explicit publication evidence, most reliable first.
_PUBLICATION_YEAR_RES = (
    re.compile(
        r"\b(?:published(?:[ \t]+online)?|available[ \t]+online|first[ \t]+published|"
        r"epub(?:lished)?(?:[ \t]+ahead[ \t]+of[ \t]+print)?)\b" + _DATE_PREFIX + _YEAR,
        re.IGNORECASE,
    ),
    re.compile(r"(?:©|\bcopyright\b)[^\n]{0,60}?" + _YEAR, re.IGNORECASE),
    re.compile(
        r"^[ \t]*(?:in[ \t]+)?(?:proceedings\b|\d{1,3}(?:st|nd|rd|th)[ \t]+(?:annual[ \t]+)?"
        r"(?:international[ \t]+)?(?:conference|symposium|workshop|meeting)\b)[^\n]{0,120}?" + _YEAR,
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"\baccepted\b" + _DATE_PREFIX + _YEAR, re.IGNORECASE),
)
_YEAR_TOKEN_RE = re.compile(_YEAR)


def _guess_year(page0: str, front: str, meta_year: Optional[int]) -> str:
    """
    Publication year, from the most to the least reliable evidence: the arXiv
    stamp's date > "published" > copyright > proceedings header > "accepted" >
    arXiv id (YYMM) on page 1 > latest year printed on page 1 > PDF creation
    date. Numbers inside DOIs and identifiers are ignored (arXiv:2010.12345 is
    October 2020, not 2010), and so are years beyond next year ("targets for 2050").
    """
    latest = datetime.date.today().year + 1

    def plausible(values) -> list[int]:
        return [year for year in (int(value) for value in values) if 1950 <= year <= latest]

    stamp = plausible(_ARXIV_STAMP_RE.findall(page0))
    if stamp:
        return str(stamp[0])
    for pattern in _PUBLICATION_YEAR_RES:
        years = plausible(pattern.findall(front))
        if years:
            return str(max(years))
    arxiv = _ARXIV_ID_DATE_RE.search(page0)
    if arxiv and 1 <= int(arxiv.group(2)) <= 12 and 2007 <= 2000 + int(arxiv.group(1)) <= latest:
        return str(2000 + int(arxiv.group(1)))
    years = plausible(_YEAR_TOKEN_RE.findall(page0))
    if years:
        return str(max(years))
    if meta_year and 1950 <= meta_year <= latest:
        return str(meta_year)
    return ""


def _guess_title(page0: str) -> str:
    """Return the first title-shaped line from the first page."""
    lines = [line.strip() for line in page0.splitlines() if line.strip()]
    skip_patterns = re.compile(
        r"^(https?://|www\.|©|copyright|\d+$|received|accepted|"
        r"published|vol\.|volume|doi:|email|e-mail|abstract$|keywords?$)",
        re.IGNORECASE,
    )
    for line in lines[:20]:
        if len(line) < 8 or len(line) > 250:
            continue
        if skip_patterns.match(line):
            continue
        alpha_ratio = sum(char.isalpha() or char.isspace() for char in line) / max(len(line), 1)
        if alpha_ratio > 0.55:
            return line
    return lines[0] if lines else ""


def _guess_authors(page0: str, title: str) -> str:
    """Return likely author lines between the title and the abstract."""
    lines = [line.strip() for line in page0.splitlines() if line.strip()]
    title_norm = _clean(title).lower()
    if not title_norm:
        return ""
    start = None
    for index, line in enumerate(lines):
        lowered = _clean(line).lower()
        # The title may wrap over several lines; skip past every one of them.
        if len(lowered) >= 4 and (lowered in title_norm or title_norm in lowered):
            start = index + 1
        elif start is not None:
            break
    if start is None:
        return ""

    author_lines: list[str] = []
    for line in lines[start:]:
        if re.match(r"abstract\b", line, re.IGNORECASE):
            break
        if len(line) > 5 and not re.match(r"^\d+\.?\s", line):
            author_lines.append(line)
        if len(author_lines) >= 3:
            break
    return "; ".join(author_lines)


_ABSTRACT_HEAD_RE = re.compile(r"^[ \t]*abstract\b[ \t]*[:.—–-]?", re.IGNORECASE | re.MULTILINE)
_KEYWORDS_HEAD_RE = re.compile(
    r"^[ \t]*(?:key[ \t]*words?|index[ \t]+terms)\b[ \t]*[:.—–-]?", re.IGNORECASE | re.MULTILINE
)
# A line that starts the next part of the paper: a numbered heading ("1 Introduction",
# "I. INTRODUCTION", "2.1 Data") or a known front-matter label. Structured-abstract
# labels (Background, Methods, Results) deliberately do NOT end an abstract.
_BLOCK_END_RE = re.compile(
    r"^[ \t]*(?:(?:\d+(?:\.\d+)*|[IVX]+)\.?[ \t]+(?-i:[A-Z])"
    r"|(?:abstract|introduction|key[ \t]*words?|index[ \t]+terms|ccs[ \t]+concepts|"
    r"acm[ \t]+reference[ \t]+format|references)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_block(text: str, head_re: re.Pattern, max_chars: int, max_lines: int = 0) -> str:
    """
    Text after a heading such as "Abstract", up to the first line that starts the
    next part of the paper. Only lines after the heading's own line can end the
    block, so "Abstract—We present ..." keeps its first sentence.
    """
    head = head_re.search(text)
    if not head:
        return ""
    rest = text[head.end():]
    newline = rest.find("\n")
    end = _BLOCK_END_RE.search(rest, newline + 1) if newline >= 0 else None
    block = rest[: end.start()] if end else rest
    if max_lines:
        block = "\n".join([line for line in block.split("\n") if line.strip()][:max_lines])
    return block.lstrip(" \t\n:.—–-").rstrip()[:max_chars]


# -----------------------------------------------------------------------------
# SQLITE INDEX
# -----------------------------------------------------------------------------

BASE_SCHEMA = """
-- Small, frequently read columns come first and the large text last. SQLite
-- stores a row's columns in order, so any column placed after a 200 KB body
-- could only be read by walking that body's overflow pages.
CREATE TABLE IF NOT EXISTS papers (
    id           INTEGER PRIMARY KEY,
    path         TEXT    UNIQUE NOT NULL,
    mtime        REAL    NOT NULL,
    size         INTEGER NOT NULL,
    filename     TEXT    NOT NULL,
    year         TEXT    DEFAULT '',
    doi          TEXT    DEFAULT '',
    arxiv_id     TEXT    DEFAULT '',
    is_scholarly INTEGER DEFAULT 1,  -- classifier verdict: genuine paper/report?
    visible      INTEGER DEFAULT 1,  -- shown in search/Collect: user override, else verdict
    has_text     INTEGER DEFAULT 1,  -- 0 = no extractable text (scanned/damaged)
    has_tail     INTEGER DEFAULT 0,  -- 1 = text beyond FULLTEXT_CHARS stored in tail
    truncated    INTEGER DEFAULT 0,  -- 1 = text beyond MAX_TEXT_CHARS was not stored
    doc_reason   TEXT    DEFAULT '', -- why it was (not) classified as scholarly
    indexed_at   TEXT    DEFAULT (datetime('now')),
    title        TEXT    DEFAULT '',
    authors      TEXT    DEFAULT '',
    keywords     TEXT    DEFAULT '',
    abstract     TEXT    DEFAULT '',
    fulltext     TEXT    DEFAULT '', -- first FULLTEXT_CHARS of the body; FTS-indexed
    tail         BLOB                -- zlib-compressed rest of the body, or NULL
);
-- The user's include/hide decisions, kept apart from papers so they survive a
-- re-index or an index format upgrade.
CREATE TABLE IF NOT EXISTS overrides (
    path    TEXT PRIMARY KEY,
    visible INTEGER NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title,
    authors,
    year,
    abstract,
    keywords,
    fulltext,
    content='papers',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, authors, year, abstract, keywords, fulltext)
    VALUES (new.id, new.title, new.authors, new.year, new.abstract, new.keywords, new.fulltext);
END;

CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors, year, abstract, keywords, fulltext)
    VALUES ('delete', old.id, old.title, old.authors, old.year, old.abstract, old.keywords, old.fulltext);
END;

CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors, year, abstract, keywords, fulltext)
    VALUES ('delete', old.id, old.title, old.authors, old.year, old.abstract, old.keywords, old.fulltext);
    INSERT INTO papers_fts(rowid, title, authors, year, abstract, keywords, fulltext)
    VALUES (new.id, new.title, new.authors, new.year, new.abstract, new.keywords, new.fulltext);
END;
"""


def _sqlite_has_fts5() -> bool:
    """Return True if this SQLite build supports FTS5 virtual tables."""
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        finally:
            probe.close()
        return True
    except sqlite3.Error:
        return False


# Probed once at import; FTS availability cannot change during a run, so there
# is no need to query sqlite_master on every search.
FTS5_AVAILABLE = _sqlite_has_fts5()

# Bump when the stored fields change in a way that requires re-extraction.
# v2 introduced whole-body full-text indexing (previously only ~4k chars).
# v3 added scholarly-document classification (is_scholarly / doc_reason).
# v4 normalises extracted text (ligatures, hyphen variants, line-break hyphens).
# v5 reads with pdfium, adds has_text, and improves title/year/classification.
# v6 adds the truncated flag and better abstract bounds.
# v7 reorders columns (large text last), stores the full text (compressed tail),
# adds user overrides, skips macOS/trash files, and recognises report issuers.
INDEX_FORMAT_VERSION = 7


def get_db_path(directory: str) -> str:
    """Index file for ``directory``: in INDEX_DIR, keyed by the folder's absolute path."""
    folder = os.path.normcase(os.path.abspath(directory))
    digest = hashlib.sha1(folder.encode("utf-8")).hexdigest()[:16]
    name = re.sub(r"[^\w.-]+", "_", os.path.basename(folder.rstrip("\\/")) or "root")[:40]
    return os.path.join(INDEX_DIR, f"{name}-{digest}.db")


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # When the format changes the papers table is rebuilt (not just emptied), so
    # its column layout is always current. The FTS table and triggers go first
    # so dropping the rows does not touch the old FTS index. Overrides are kept.
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < INDEX_FORMAT_VERSION:
        if version:  # a brand-new database has nothing to re-index
            LOGGER.info("Index format v%d -> v%d; rebuilding the index.", version, INDEX_FORMAT_VERSION)
        for trigger in ("papers_ai", "papers_ad", "papers_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        with suppress(sqlite3.OperationalError):  # fts5 module may be missing
            conn.execute("DROP TABLE IF EXISTS papers_fts")
        conn.execute("DROP TABLE IF EXISTS papers")
        conn.execute(f"PRAGMA user_version = {INDEX_FORMAT_VERSION}")
    conn.executescript(BASE_SCHEMA)

    if FTS5_AVAILABLE:
        fts_existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers_fts'"
        ).fetchone() is not None
        conn.executescript(FTS_SCHEMA)
        # A freshly created FTS table over existing rows (e.g. a DB first built
        # without FTS5) must be filled. Checking existence is O(1); counting FTS
        # rows on every open was O(index size).
        if not fts_existed and conn.execute("SELECT EXISTS(SELECT 1 FROM papers)").fetchone()[0]:
            conn.execute("INSERT INTO papers_fts(papers_fts) VALUES ('rebuild')")
    else:
        LOGGER.warning("FTS5 unavailable in this SQLite build; using SQL fallback search.")
    conn.commit()


def open_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_MS / 1000)
    _configure_connection(conn)
    _ensure_schema(conn)
    return conn


def open_reader(db_path: str) -> sqlite3.Connection:
    """Read-only connection for the search thread (the schema is ensured by open_db)."""
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_MS}")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")
    return conn


_UPSERT_COLUMNS = (
    "path", "mtime", "size", "filename", "year", "doi", "arxiv_id", "is_scholarly", "visible",
    "has_text", "has_tail", "truncated", "doc_reason", "title", "authors", "keywords",
    "abstract", "fulltext", "tail",
)
_UPSERT_SQL = (
    f"INSERT INTO papers ({', '.join(_UPSERT_COLUMNS)}, indexed_at) VALUES ("
    + ", ".join(
        # A user override wins over the classifier for what is visible.
        "COALESCE((SELECT visible FROM overrides WHERE path = :path), :is_scholarly)"
        if column == "visible" else f":{column}"
        for column in _UPSERT_COLUMNS
    )
    + ", datetime('now')) ON CONFLICT(path) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in (*_UPSERT_COLUMNS[1:], "indexed_at"))
)


def upsert_paper(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    size: int,
    fields: dict,
) -> None:
    tail = fields.get("tail") or None
    conn.execute(_UPSERT_SQL, {
        "path": path,
        "mtime": mtime,
        "size": size,
        "filename": os.path.basename(path),
        # Callers that seed rows directly (not via extraction) default to
        # scholarly so they are not silently hidden from search.
        "is_scholarly": int(fields.get("is_scholarly", 1)),
        "visible": None,  # computed in SQL
        "has_text": int(fields.get("has_text", 1)),
        "has_tail": int(tail is not None),
        "truncated": int(fields.get("truncated", 0)),
        "doc_reason": str(fields.get("doc_reason", "")),
        "tail": tail,
        **{key: fields.get(key, "") for key in
           ("year", "doi", "arxiv_id", "title", "authors", "keywords", "abstract", "fulltext")},
    })


def set_visibility(conn: sqlite3.Connection, path: str, visible: bool) -> None:
    """
    Show or hide one document in search and Collect, overriding the classifier.
    Choosing what the classifier decided anyway removes the override, so the
    document follows the classifier again if it is re-indexed later.
    """
    row = conn.execute("SELECT is_scholarly FROM papers WHERE path = ?", (path,)).fetchone()
    with conn:
        if row is not None and bool(row[0]) == visible:
            conn.execute("DELETE FROM overrides WHERE path = ?", (path,))
        else:
            conn.execute("INSERT OR REPLACE INTO overrides (path, visible) VALUES (?, ?)", (path, int(visible)))
        conn.execute("UPDATE papers SET visible = ? WHERE path = ?", (int(visible), path))


def count_nonscholarly(conn: sqlite3.Connection) -> int:
    """How many readable documents are hidden as non-papers."""
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM papers WHERE visible = 0 AND has_text = 1"
        ).fetchone()[0]
    except sqlite3.Error:
        return 0


def count_textless(conn: sqlite3.Connection) -> int:
    """How many PDFs had no extractable text (scanned images or damaged files)."""
    try:
        return conn.execute("SELECT COUNT(*) FROM papers WHERE has_text = 0").fetchone()[0]
    except sqlite3.Error:
        return 0


# Folders that never hold a user's papers: recycle bins, OS metadata, chkdsk
# leftovers. Dot-folders (.Trashes, .Spotlight-V100, .git, ...) are skipped too.
_SKIP_DIR_NAMES = {"$recycle.bin", "recycler", "system volume information", "lost+found"}
_CHKDSK_DIR_RE = re.compile(r"^found\.\d{3}$", re.IGNORECASE)


def _skip_directory(name: str, path: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(".") or lowered in _SKIP_DIR_NAMES or _CHKDSK_DIR_RE.match(lowered):
        return True
    if sys.platform == "win32":  # hidden + system: Windows-managed folders
        with suppress(OSError, AttributeError):
            attributes = os.stat(path, follow_symlinks=False).st_file_attributes
            wanted = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
            return attributes & wanted == wanted
    return False


def iter_pdf_files(directory: str) -> list[Path]:
    """
    Every PDF under ``directory``, skipping recycle bins and OS folders, and
    macOS "._name.pdf" files: those are 4 KB resource-fork companions written on
    non-Mac drives, not PDFs, and would otherwise be reported as unreadable.
    """
    pdf_paths: list[Path] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if not _skip_directory(name, os.path.join(root, name))]
        for name in files:
            if name.lower().endswith(".pdf") and not name.startswith("._"):
                pdf_paths.append(Path(root) / name)
    pdf_paths.sort(key=lambda path: str(path).lower())
    return pdf_paths


# -----------------------------------------------------------------------------
# INDEXING
# -----------------------------------------------------------------------------

class IndexStats(NamedTuple):
    total: int
    indexed: int
    skipped: int
    errors: int
    cancelled: bool
    error_samples: list[str]


def _resolve_worker_count(workers: Optional[int]) -> int:
    """Resolve the number of extraction worker processes (0/None => auto)."""
    if workers is None:
        workers = INDEX_WORKERS
    if workers <= 0:
        workers = min(os.cpu_count() or 1, 8)
    return max(1, workers)


def index_directory(
    directory: str,
    db_path: str,
    *,
    force_reindex: bool = False,
    workers: Optional[int] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> IndexStats:
    """
    Index every PDF under ``directory`` into the SQLite database at ``db_path``.

    PDF text extraction (the slow, CPU-bound part) is fanned out across a process
    pool; database writes stay on this single thread because SQLite serializes
    them anyway. ``progress(done, total, name)`` is invoked as files complete and
    ``should_cancel()`` is polled so the caller can interrupt a long run.
    """
    report = progress or (lambda *_: None)
    cancel = should_cancel or (lambda: False)

    error_samples: list[str] = []
    processed: set[str] = set()
    buffered: list[tuple[str, float, int, dict]] = []
    indexed = errors = done = 0
    cancelled = False

    conn = open_db(db_path)
    try:
        pdf_files = iter_pdf_files(directory)
        total = len(pdf_files)
        existing_paths = {str(path) for path in pdf_files}

        # One query for the whole library instead of one lookup per file.
        if force_reindex:
            conn.execute("DELETE FROM papers")
            known: dict[str, tuple[float, int]] = {}
        else:
            known = {row["path"]: (row["mtime"], row["size"])
                     for row in conn.execute("SELECT path, mtime, size FROM papers")}
            missing = known.keys() - existing_paths
            if missing:
                conn.executemany("DELETE FROM papers WHERE path = ?", ((path,) for path in missing))
        conn.commit()

        # Decide which files actually need work before spending any CPU on them.
        to_index: list[tuple[str, float, int]] = []
        for pdf_path in pdf_files:
            try:
                stat = pdf_path.stat()
            except OSError:
                continue
            path_str = str(pdf_path)
            cached = known.get(path_str)
            if cached is None or abs(cached[0] - stat.st_mtime) > 0.01 or cached[1] != stat.st_size:
                to_index.append((path_str, stat.st_mtime, stat.st_size))
        skipped = total - len(to_index)

        def flush() -> None:
            # Rows are written in one short transaction per batch, so the write
            # lock is never held while waiting on extraction and other writers
            # (Collect, a second window) don't time out with "database is locked".
            if buffered:
                with conn:
                    for args in buffered:
                        upsert_paper(conn, *args)
                buffered.clear()

        def store(path_str: str, mtime: float, size: int, fields, exc) -> None:
            nonlocal indexed, errors, done
            processed.add(path_str)
            done += 1
            report(done, len(to_index), os.path.basename(path_str))
            if exc is not None:
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{os.path.basename(path_str)}: {exc}")
                LOGGER.warning("Failed to index %s: %s", path_str, exc)
                return
            buffered.append((path_str, mtime, size, fields))
            indexed += 1
            if len(buffered) >= INDEX_COMMIT_BATCH:
                flush()

        worker_count = _resolve_worker_count(workers)
        use_pool = worker_count > 1 and len(to_index) > 1

        if use_pool:
            try:
                with ProcessPoolExecutor(max_workers=worker_count) as executor:
                    futures = {
                        executor.submit(_extract_fields, path_str): (path_str, mtime, size)
                        for path_str, mtime, size in to_index
                    }
                    for future in as_completed(futures):
                        if cancel():
                            cancelled = True
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        # pop + del so each (now large) body result is freed
                        # right after storing, not retained until the loop ends.
                        path_str, mtime, size = futures.pop(future)
                        try:
                            store(path_str, mtime, size, future.result(), None)
                        except Exception as exc:  # extraction raised in a worker
                            store(path_str, mtime, size, None, exc)
                        del future
            except Exception:
                LOGGER.exception("Process pool unavailable; using serial extraction")
                use_pool = False

        if not use_pool and not cancelled:
            for path_str, mtime, size in to_index:
                if path_str in processed:
                    continue
                if cancel():
                    cancelled = True
                    break
                try:
                    store(path_str, mtime, size, _extract_fields(path_str), None)
                except Exception as exc:
                    store(path_str, mtime, size, None, exc)

        flush()  # also on cancel: keep the work already done
        if not cancelled:
            with suppress(sqlite3.Error):
                conn.execute("PRAGMA optimize")
        return IndexStats(total, indexed, skipped, errors, cancelled, error_samples)
    finally:
        with suppress(sqlite3.Error):
            conn.close()


# -----------------------------------------------------------------------------
# SEARCH ENGINE
# -----------------------------------------------------------------------------

def _split_terms(query: str) -> list[str]:
    query = normalize_text(query)
    return [term.lower() for term in re.split(r"[\s,;]+", query.strip()) if len(term) >= 2]


def _fts_tokens(terms: list[str]) -> list[str]:
    """
    Prefix tokens for FTS5. Splitting uses Unicode-aware ``\\W`` (not ASCII
    ``[^0-9A-Za-z]``), so "schrödinger" stays one token instead of becoming
    "schr" OR "dinger"; FTS5's unicode61 tokenizer folds the diacritics.
    """
    tokens: list[str] = []
    for term in terms:
        for token in re.split(r"[\W_]+", term):
            if len(token) >= 2:
                tokens.append(f'"{token}"*')
    return list(dict.fromkeys(tokens))


def _fuzzy_score(query: str, text: str) -> float:
    """Return a 0-100 fuzzy match score."""
    if not text:
        return 0.0
    if RAPIDFUZZ_OK:
        return max(
            fuzz.partial_ratio(query, text),
            fuzz.token_set_ratio(query, text) * 0.9,
        )
    return difflib.SequenceMatcher(None, query.lower(), text.lower()).ratio() * 100


YEAR_BOUND_MIN, YEAR_BOUND_MAX = 1000, 9999


def _year_int(text: str, default: Optional[int]) -> Optional[int]:
    text = text.strip()
    if not text:
        return default
    return int(text) if text.isdigit() else None


def parse_year_range(raw: Optional[str]) -> Optional[tuple[int, int]]:
    """
    Parse a year filter into an inclusive ``(low, high)`` range, or ``None``.

    Accepts a single year (``"2024"``), an explicit range (``"2024-2025"``), or an
    open-ended range (``"2020-"`` for 2020 onward, ``"-2019"`` up to 2019).
    ``-``, en/em dashes, ``..`` and ``to`` all work as separators. Invalid input
    yields ``None`` (no year filter) rather than raising.
    """
    if not raw:
        return None
    text = re.sub(r"\s*(?:–|—|\.\.|to)\s*", "-", raw.strip(), flags=re.IGNORECASE)
    if "-" in text:
        low_text, _, high_text = text.partition("-")
        low = _year_int(low_text, YEAR_BOUND_MIN)
        high = _year_int(high_text, YEAR_BOUND_MAX)
    else:
        low = high = _year_int(text, None)
    if low is None or high is None:
        return None
    return (low, high) if low <= high else (high, low)


# One weight per FTS column, in declared order: title, authors, year, abstract,
# keywords, fulltext.
_BM25 = "bm25(papers_fts, 5.0, 2.0, 1.0, 3.0, 4.0, 1.0)"
_ID_CHUNK = 400  # ids per "WHERE id IN (...)" lookup


def _ranked_rows(conn, match: str, filters: str, params: list, need: int, exclude: set) -> list:
    """
    Up to ``need`` rows matching ``match``, best first. Ranking runs on the FTS
    index alone (cheap); full rows are then fetched for the best ids only, in
    chunks, until enough pass the filters. Joining every match before sorting
    read thousands of rows per keystroke on a common word.
    """
    ids = [rowid for (rowid,) in conn.execute(
        f"SELECT rowid FROM papers_fts WHERE papers_fts MATCH ? ORDER BY {_BM25}", [match]
    ) if rowid not in exclude]
    rows: list = []
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        found = conn.execute(
            f"SELECT {_ROW_SELECT} FROM papers WHERE id IN ({','.join('?' * len(chunk))}){filters}",
            [*chunk, *params],
        ).fetchall()
        position = {rowid: index for index, rowid in enumerate(chunk)}
        rows.extend(sorted(found, key=lambda row: position[row["id"]]))
        if len(rows) >= need:
            break
    return rows[:need]


def _fetch_candidate_rows(
    conn: sqlite3.Connection,
    terms: list[str],
    year_range: Optional[tuple[int, int]],
    top_n: int,
) -> tuple[list[sqlite3.Row], int]:
    """Candidates best-first, plus how many leading rows contain every term."""
    limit = max(SEARCH_CANDIDATE_LIMIT, top_n * 10)
    filters, params = "", []
    if year_range:
        filters += " AND CAST(year AS INTEGER) BETWEEN ? AND ?"
        params.extend(year_range)
    if REQUIRE_SCHOLARLY:
        filters += " AND visible = 1"

    if FTS5_AVAILABLE:
        tokens = _fts_tokens(terms)
        if tokens:
            try:
                # Papers containing ALL terms come first; papers with ANY term
                # only fill up the list when there are not enough full matches.
                # The FTS answer is final, empty or not: falling back to a
                # substring scan on "no results" read the whole library's text.
                rows = _ranked_rows(conn, " AND ".join(tokens), filters, params, limit, set())
                full_matches = len(rows)
                if full_matches < top_n and len(tokens) > 1:
                    seen = {row["id"] for row in rows}
                    rows += _ranked_rows(conn, " OR ".join(tokens), filters, params, limit - full_matches, seen)
                return rows, full_matches
            except sqlite3.OperationalError:
                LOGGER.warning("FTS query failed; falling back to SQL scan.", exc_info=True)

    term_clauses: list[str] = []
    for term in terms:
        field_checks = [f"instr(lower({field}), ?) > 0" for field in SEARCH_FIELDS]
        term_clauses.append("(" + " OR ".join(field_checks) + ")")
        params.extend([term] * len(SEARCH_FIELDS))
    sql = f"SELECT {_ROW_SELECT} FROM papers WHERE 1{filters}"
    if term_clauses:
        sql += " AND (" + " OR ".join(term_clauses) + ")"
    rows = conn.execute(sql + " LIMIT ?", [*params, limit]).fetchall()
    return rows, len(rows)


# Unicode combining marks (accents), removed after NFKD decomposition.
_COMBINING_RE = re.compile("[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")


def _fold(text: str) -> str:
    """Lower-case and strip diacritics, matching how the FTS index compares text."""
    text = text.lower()
    if text.isascii():
        return text
    return _COMBINING_RE.sub("", unicodedata.normalize("NFKD", text))


@functools.lru_cache(maxsize=256)
def _word_start_pattern(term: str) -> re.Pattern:
    # The term must begin a word: "atten" matches "attention", "ion" does not.
    return re.compile(r"(?<!\w)" + re.escape(_fold(term)))


def score_paper(terms: list[str], row: sqlite3.Row) -> float:
    """
    Bonus score from exact/fuzzy matches in the short, high-signal fields. This
    refines the BM25 candidate order; it does not gate results (a paper whose
    only match is deep in the body scores 0 here but is still a valid hit).
    """
    total = 0.0
    fields = {name: (row[name] or "") for name in SCORE_WEIGHTS}
    folded_fields = {name: _fold(value) for name, value in fields.items()}

    for term in terms:
        pattern = _word_start_pattern(term)
        for name, weight in SCORE_WEIGHTS.items():
            if pattern.search(folded_fields[name]):
                total += weight * 100
                continue
            if name in ("title", "abstract", "keywords"):
                fuzzy_score = _fuzzy_score(term, fields[name][:500])
                if fuzzy_score > 60:
                    total += weight * fuzzy_score * 0.5
    return total


def search(
    conn: sqlite3.Connection,
    query: str,
    year_filter=None,
    top_n: int = 30,
) -> list[dict[str, object]]:
    """
    Search indexed papers and return sorted result dictionaries. ``year_filter``
    may be a raw string ("2024", "2020-2025", "2020-", "-2019") or a pre-parsed
    ``(low, high)`` tuple.
    """
    terms = _split_terms(query)
    if not terms:
        return []

    year_range = year_filter if isinstance(year_filter, tuple) else parse_year_range(year_filter)
    rows, full_matches = _fetch_candidate_rows(conn, terms, year_range, top_n=top_n)
    # Candidates arrive best-first (BM25 for FTS). Keep that as a base score so a
    # paper matched only deep in the body is retained, then let the field-level
    # bonus lift exact title/keyword hits above it. Papers containing every term
    # form a tier of their own that partial matches can never outrank.
    total_rows = len(rows)
    full: list[tuple[float, dict[str, object]]] = []
    partial: list[tuple[float, dict[str, object]]] = []
    for position, row in enumerate(rows):
        scored = (total_rows - position + score_paper(terms, row), dict(row))
        (full if position < full_matches else partial).append(scored)

    def rank(items: list, low: int, high: int) -> list[dict[str, object]]:
        items.sort(key=lambda item: -item[0])
        best = max(items[0][0], 1) if items else 1
        for score, result in items:
            result["relevance"] = int(low + (high - low) * score / best)
        return [result for _, result in items]

    if full and partial:  # full matches 50-100%, partial matches below them
        return (rank(full, 50, 100) + rank(partial, 0, 50))[:top_n]
    return rank(full or partial, 0, 100)[:top_n]


class SearchWorker(threading.Thread):
    """
    Runs searches off the UI thread, on its own read-only connection. Requests
    are coalesced: while one search runs, only the newest pending request is
    kept, so fast typing never queues stale work. Each outcome is delivered as
    ``on_result(seq, results, error)`` on this thread; callers marshal it to
    their UI and drop results whose ``seq`` is no longer current.
    """

    def __init__(self, on_result: Callable[[int, Optional[list], Optional[Exception]], None]):
        super().__init__(daemon=True, name="search")
        self._on_result = on_result
        self._requests: queue.Queue = queue.Queue()

    def submit(self, db_path: str, query: str, year_filter, seq: int) -> None:
        self._requests.put((db_path, query, year_filter, seq))

    def stop(self) -> None:
        self._requests.put(None)

    def run(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        conn_path: Optional[str] = None
        try:
            while True:
                request = self._requests.get()
                while request is not None:  # skip to the newest request
                    try:
                        request = self._requests.get_nowait()
                    except queue.Empty:
                        break
                if request is None:
                    return
                db_path, query, year_filter, seq = request
                try:
                    if db_path != conn_path:
                        if conn is not None:
                            conn.close()
                        conn, conn_path = None, None
                        conn, conn_path = open_reader(db_path), db_path
                    results = search(conn, query, year_filter=year_filter)
                except Exception as exc:  # report and keep serving
                    self._on_result(seq, None, exc)
                else:
                    self._on_result(seq, results, None)
        finally:
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.close()


# -----------------------------------------------------------------------------
# EXACT-MATCH AGGREGATION
#
# Collect every paper in a source folder that *exactly* contains ALL of the
# given keywords into a destination folder. Correctness is paramount here, so
# the fuzzy/prefix/BM25 interactive search is deliberately NOT used as the gate:
# FTS only narrows candidates quickly; a strict whole-word regex makes the final
# decision, guaranteeing zero false positives (no substring, stem, or fuzzy hits).
# -----------------------------------------------------------------------------

# Fields (in the base table) whose text is checked for an exact keyword match.
MATCH_FIELDS = ("title", "authors", "abstract", "keywords", "fulltext")


class AggregateStats(NamedTuple):
    total_indexed: int
    candidates: int
    matched: int
    copied: int
    skipped_existing: int   # identical paper already present in the destination
    skipped_nonpaper: int   # matched keywords but discarded as a non-scholarly doc
    errors: int
    cancelled: bool
    dest_dir: str
    unreadable: int = 0     # PDFs with no extractable text: could not be checked
    # Copied, but sharing a DOI/arXiv id with a paper already in the destination
    # (e.g. preprint vs. published version): "new.pdf ~ old.pdf (same DOI)".
    possible_duplicates: tuple = ()


def parse_keywords(raw: str) -> list[str]:
    """Split a keyword string on commas / newlines / semicolons; keep phrases."""
    parts = re.split(r"[,\n;]+", raw or "")
    seen: dict[str, None] = {}
    for part in parts:
        cleaned = " ".join(part.split())  # collapse internal whitespace
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def compile_keyword_patterns(keywords: list[str], match_case: bool) -> list[re.Pattern]:
    """
    One whole-word/phrase regex per keyword. Word boundaries are enforced with
    lookarounds so a keyword only matches as a standalone token (e.g. "ion" does
    NOT match "ionization"), eliminating substring false positives. Multi-word
    keywords tolerate variable inter-word whitespace only.
    """
    flags = 0 if match_case else re.IGNORECASE
    patterns: list[re.Pattern] = []
    for keyword in keywords:
        # Normalise exactly like the indexed text, or a keyword typed with a
        # compatibility character could never match.
        tokens = normalize_text(keyword).split()
        if not tokens:
            continue
        body = r"\s+".join(re.escape(token) for token in tokens)
        patterns.append(re.compile(rf"(?<!\w){body}(?!\w)", flags))
    return patterns


def paper_matches(row, patterns: list[re.Pattern]) -> bool:
    """
    A paper matches only if EVERY keyword is found, each wholly inside a single
    field. Fields are searched separately so a phrase can never be stitched
    together across a field boundary (e.g. a title ending in "Machine" followed
    by authors starting with "Learning").
    """
    texts = [str(row[field] or "") for field in MATCH_FIELDS]
    return all(any(pattern.search(text) for text in texts) for pattern in patterns)


_CANDIDATE_COLUMNS = ", ".join(
    f"papers.{column}"
    for column in ("id", "path", "visible", "has_tail", "truncated", "doi", "arxiv_id", *MATCH_FIELDS)
)


def _candidate_rows(conn: sqlite3.Connection, keywords: list[str]) -> list[sqlite3.Row]:
    """
    Narrow to papers that plausibly contain all keywords. FTS does this fast over
    huge collections; the caller still verifies each hit exactly. Papers longer
    than the FTS-indexed part are always included, because a keyword beyond it
    is invisible to FTS. Falls back to a full scan when FTS5 is unavailable
    (correctness is unaffected either way).
    """
    rows = None
    if FTS5_AVAILABLE:
        phrases = []
        for keyword in keywords:
            tokens = re.sub(r'"', " ", keyword).split()
            if tokens:
                phrases.append('"' + " ".join(tokens) + '"')
        if phrases:
            try:
                rows = conn.execute(
                    f"SELECT {_CANDIDATE_COLUMNS} FROM papers_fts "
                    "JOIN papers ON papers.id = papers_fts.rowid WHERE papers_fts MATCH ?",
                    [" AND ".join(phrases)],
                ).fetchall()
            except sqlite3.OperationalError:
                LOGGER.warning("Aggregation FTS query failed; scanning all rows.", exc_info=True)
    if rows is None:
        return conn.execute(f"SELECT {_CANDIDATE_COLUMNS} FROM papers").fetchall()
    seen = {row["id"] for row in rows}
    longer = conn.execute(f"SELECT {_CANDIDATE_COLUMNS} FROM papers WHERE has_tail = 1 OR truncated = 1")
    return rows + [row for row in longer if row["id"] not in seen]


def _complete_text_row(conn: sqlite3.Connection, row) -> dict:
    """
    The row with ``fulltext`` extended to the whole document: the stored,
    compressed tail is appended (no PDF access). Only a document longer than
    MAX_TEXT_CHARS, whose stored text is itself incomplete, is re-read.
    """
    full = dict(row)
    if row["truncated"]:
        extracted = _extract_pdf_content(row["path"], max_front_pages=1, char_limit=0)
        if extracted.body:
            full["fulltext"] = _clean(extracted.body)
        return full
    (tail,) = conn.execute("SELECT tail FROM papers WHERE id = ?", (row["id"],)).fetchone()
    if tail:
        full["fulltext"] = row["fulltext"] + zlib.decompress(tail).decode("utf-8")
    return full


def _hash_file(path: str, chunk: int = 1 << 20) -> str:
    """Content hash used to recognise a paper already present in the destination."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _free_name(base: str, dest_dir: str, taken: set[str]) -> str:
    """``base``, or "stem (n).ext" if that name is already used in ``dest_dir``."""
    stem, ext = os.path.splitext(base)
    name, counter = base, 1
    while name in taken or os.path.exists(os.path.join(dest_dir, name)):
        name = f"{stem} ({counter}){ext}"
        counter += 1
    return name


def _copy_into(paths: list[str], dest_dir: str) -> tuple[list[tuple[str, str]], int, int]:
    """
    Copy files into ``dest_dir``, skipping any whose bytes are already there and
    renaming merely-colliding names. A file is hashed only when another file of
    the same size exists (in the destination or copied earlier in this run), so
    the common case does no hashing at all. Returns ``(copied, errors,
    skipped_existing)``, where ``copied`` lists ``(source path, new file name)``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    by_size: dict[int, list[str]] = {}
    names: set[str] = set()
    for entry in os.scandir(dest_dir):
        if entry.is_file():
            names.add(entry.name)
            with suppress(OSError):
                by_size.setdefault(entry.stat().st_size, []).append(entry.path)

    digests: dict[str, Optional[str]] = {}

    def digest_of(path: str) -> Optional[str]:
        if path not in digests:
            try:
                digests[path] = _hash_file(path)
            except OSError:
                digests[path] = None
        return digests[path]

    copied: list[tuple[str, str]] = []
    errors = skipped_existing = 0
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            errors += 1
            LOGGER.warning("Could not read %s: %s", path, exc)
            continue
        same_size = by_size.get(size, [])
        if same_size:
            digest = digest_of(path)
            if digest is not None and any(digest_of(other) == digest for other in same_size):
                skipped_existing += 1
                continue
        name = _free_name(os.path.basename(path), dest_dir, names)
        target = os.path.join(dest_dir, name)
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            errors += 1
            LOGGER.warning("Could not copy %s: %s", path, exc)
            continue
        names.add(name)
        by_size.setdefault(size, []).append(target)
        if path in digests:
            digests[target] = digests[path]  # identical bytes
        copied.append((path, name))
    return copied, errors, skipped_existing


def _identifier_keys(doi: str, arxiv_id: str) -> list[str]:
    keys = []
    if doi:
        keys.append("doi:" + doi.lower())
    if arxiv_id:
        keys.append("arxiv:" + arxiv_id.lower())
    return keys


def _destination_identifiers(
    dest_dir: str, workers: Optional[int], progress: Callable[[int, int, str], None]
) -> dict[str, str]:
    """DOI / arXiv id -> file name for papers already in the destination folder."""
    if not os.path.isdir(dest_dir):
        return {}
    db_path = get_db_path(dest_dir)
    index_directory(dest_dir, db_path, workers=workers, progress=progress)  # incremental, cached
    conn = open_db(db_path)
    try:
        identifiers: dict[str, str] = {}
        for row in conn.execute("SELECT filename, doi, arxiv_id FROM papers"):
            for key in _identifier_keys(row["doi"], row["arxiv_id"]):
                identifiers.setdefault(key, row["filename"])
        return identifiers
    finally:
        with suppress(sqlite3.Error):
            conn.close()


def aggregate_matches(
    source_dir: str,
    dest_dir: str,
    keywords: list[str],
    *,
    match_case: bool = False,
    workers: Optional[int] = None,
    progress: Optional[Callable[[str, int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> AggregateStats:
    """
    Copy every paper under ``source_dir`` that exactly contains ALL ``keywords``
    into ``dest_dir``. ``progress(phase, done, total, name)`` reports the
    "index", "match" and "dest" (reading the destination's existing papers)
    phases; ``should_cancel()`` allows interruption.
    """
    report = progress or (lambda *_: None)
    cancel = should_cancel or (lambda: False)

    patterns = compile_keyword_patterns(keywords, match_case)
    if not patterns:
        return AggregateStats(0, 0, 0, 0, 0, 0, 0, False, dest_dir)

    db_path = get_db_path(source_dir)
    index_stats = index_directory(
        source_dir,
        db_path,
        workers=workers,
        progress=lambda done, total, name: report("index", done, total, name),
        should_cancel=should_cancel,
    )
    if index_stats.cancelled:
        return AggregateStats(index_stats.total, 0, 0, 0, 0, 0, index_stats.errors, True, dest_dir)

    dest_prefix = os.path.abspath(dest_dir) + os.sep
    conn = open_db(db_path)
    matched: list[tuple[str, str, str]] = []  # (path, doi, arxiv_id)
    skipped_nonpaper = 0
    cancelled = False
    try:
        # Scanned/damaged PDFs can never match; report them so a "no match" is
        # never mistaken for "checked and absent".
        unreadable = count_textless(conn)
        rows = _candidate_rows(conn, keywords)
        candidates = len(rows)
        for position, row in enumerate(rows, start=1):
            if cancel():
                cancelled = True
                break
            report("match", position, candidates, "")
            path = row["path"]
            # Never re-collect files already sitting in the destination folder.
            if os.path.abspath(path).startswith(dest_prefix):
                continue
            # The FTS-indexed text decides most papers; the rest of a long paper
            # is only decompressed when that part alone does not match.
            hit = paper_matches(row, patterns) or (
                bool(row["has_tail"] or row["truncated"])
                and paper_matches(_complete_text_row(conn, row), patterns)
            )
            if hit:
                # Only genuine papers/reports are collected; the rest are counted
                # so the user can be told they were excluded.
                if REQUIRE_SCHOLARLY and not row["visible"]:
                    skipped_nonpaper += 1
                else:
                    matched.append((path, row["doi"] or "", row["arxiv_id"] or ""))
    finally:
        with suppress(sqlite3.Error):
            conn.close()

    if cancelled:
        return AggregateStats(index_stats.total, candidates, len(matched), 0,
                              0, skipped_nonpaper, index_stats.errors, True, dest_dir, unreadable)

    # Same paper, different file (e.g. arXiv preprint vs. publisher PDF): copy it,
    # but flag it, since a DOI/arXiv id read from page 1-2 is strong but not proof.
    keys_by_path = {path: _identifier_keys(doi, arxiv) for path, doi, arxiv in matched}
    known_ids = (
        _destination_identifiers(dest_dir, workers, lambda done, total, name: report("dest", done, total, name))
        if any(keys_by_path.values()) else {}
    )

    copied, copy_errors, skipped_existing = _copy_into([path for path, _, _ in matched], dest_dir)
    flags: list[str] = []
    for source, name in copied:
        keys = keys_by_path.get(source, [])
        for key in keys:
            other = known_ids.get(key)
            if other and other != name:
                flags.append(f"{name} ~ {other} (same {'DOI' if key.startswith('doi:') else 'arXiv id'})")
                break
        for key in keys:
            known_ids.setdefault(key, name)  # also catch pairs within this run
    return AggregateStats(
        index_stats.total, candidates, len(matched), len(copied),
        skipped_existing, skipped_nonpaper, index_stats.errors + copy_errors,
        False, dest_dir, unreadable, tuple(flags),
    )


# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------

THEME = {
    "bg": "#F8F5EE",
    "surface": "#F3EDE2",
    "card": "#FFFDF8",
    "card_hover": "#F6F0E6",
    "accent": "#C86A3A",
    "accent_soft": "#EED7CA",
    "accent2": "#8E5D41",
    "text": "#2E2823",
    "subtext": "#6C6258",
    "highlight": "#B7791F",
    "green": "#2F855A",
    "red": "#C05621",
    "border": "#E5D8C8",
    "bar_bg": "#E7DDD0",
    "bar_fill": "#C86A3A",
    "entry_bg": "#FFFCF7",
    "font_main": ("Segoe UI", 11),
    "font_title": ("Segoe UI Semibold", 12),
    "font_small": ("Segoe UI", 9),
    "font_head": ("Segoe UI Semibold", 15),
    "font_mono": ("Consolas", 9),
}


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        try:
            x, y = self.widget.winfo_pointerxy()
        except tk.TclError:
            x = self.widget.winfo_rootx() + 16
            y = self.widget.winfo_rooty() + 16
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x + 14}+{y + 14}")
        tk.Label(
            self.tip,
            text=self.text,
            bg="#FFF9E8",
            fg=THEME["text"],
            relief="solid",
            bd=1,
            font=THEME["font_small"],
            padx=6,
            pady=3,
        ).pack()

    def hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollableFrame(tk.Frame):
    """A vertically scrollable container."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=THEME["bg"], **kwargs)
        self.canvas = tk.Canvas(self, bg=THEME["bg"], highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=THEME["bg"])

        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfig(self.inner_id, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        else:
            self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")


class ResultCard(tk.Frame):
    """A single result card widget."""

    def __init__(self, parent, result: dict, on_open, on_reveal, on_hide=None, **kwargs):
        super().__init__(
            parent,
            bg=THEME["card"],
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=1,
            **kwargs,
        )
        self.result = result
        self.on_open = on_open
        self.on_reveal = on_reveal
        self.on_hide = on_hide
        self._build(result)
        self.bind("<Enter>", self._hover_on, add="+")
        self.bind("<Leave>", self._hover_off, add="+")
        self.bind("<Double-Button-1>", lambda _event: on_open(result["path"]), add="+")
        # Right-click (Ctrl-click / two-finger click on macOS) anywhere on the card.
        for sequence in ("<Button-3>", "<Button-2>") if sys.platform == "darwin" else ("<Button-3>",):
            self._bind_all_descendants(self, sequence, self._show_menu)

    def _bind_all_descendants(self, widget: tk.Widget, sequence: str, handler) -> None:
        widget.bind(sequence, handler, add="+")
        for child in widget.winfo_children():
            self._bind_all_descendants(child, sequence, handler)

    def _show_menu(self, event) -> None:
        path = self.result["path"]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open PDF", command=lambda: self.on_open(path))
        menu.add_command(label="Reveal in file manager", command=lambda: self.on_reveal(path))
        if self.on_hide:
            menu.add_separator()
            menu.add_command(label="Hide from results (not a paper/report)", command=lambda: self.on_hide(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build(self, result: dict) -> None:
        top = tk.Frame(self, bg=THEME["card"])
        top.pack(fill="x", padx=12, pady=(10, 4))

        relevance = result.get("relevance", 0)
        bar_frame = tk.Frame(top, bg=THEME["bar_bg"], width=88, height=8)
        bar_frame.pack_propagate(False)
        bar_frame.pack(side="left", padx=(0, 10), pady=4)
        fill_width = max(4, int(88 * relevance / 100))
        bar_color = (
            THEME["green"]
            if relevance > 70
            else THEME["accent"]
            if relevance > 40
            else THEME["subtext"]
        )
        tk.Frame(bar_frame, bg=bar_color, width=fill_width, height=8).place(x=0, y=0)
        Tooltip(bar_frame, f"Relevance: {relevance}%")

        year = result.get("year", "")
        if year:
            tk.Label(
                top,
                text=year,
                bg=THEME["card"],
                fg=THEME["accent2"],
                font=THEME["font_small"],
            ).pack(side="left", padx=(0, 8))

        btn_open = tk.Button(
            top,
            text="Open PDF",
            bg=THEME["accent"],
            fg="white",
            font=THEME["font_small"],
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            activebackground="#B85E31",
            activeforeground="white",
            command=lambda: self.on_open(result["path"]),
        )
        btn_open.pack(side="right", padx=2)

        btn_reveal = tk.Button(
            top,
            text="Reveal",
            bg=THEME["surface"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
            bd=0,
            padx=8,
            pady=4,
            cursor="hand2",
            activebackground=THEME["accent_soft"],
            activeforeground=THEME["text"],
            command=lambda: self.on_reveal(result["path"]),
        )
        btn_reveal.pack(side="right", padx=2)
        Tooltip(btn_reveal, "Reveal in file manager")

        title = result.get("title") or os.path.basename(result["path"])
        title_label = tk.Label(
            self,
            text=title,
            bg=THEME["card"],
            fg=THEME["text"],
            font=THEME["font_title"],
            wraplength=720,
            justify="left",
            anchor="w",
        )
        title_label.pack(fill="x", padx=12, pady=(0, 3))

        authors = result.get("authors", "")
        if authors:
            tk.Label(
                self,
                text=authors,
                bg=THEME["card"],
                fg=THEME["subtext"],
                font=THEME["font_small"],
                wraplength=720,
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 6))

        keywords = result.get("keywords", "")
        if keywords:
            keywords_frame = tk.Frame(self, bg=THEME["card"])
            keywords_frame.pack(fill="x", padx=12, pady=(0, 6))
            for chip in re.split(r"[;,·]", keywords)[:6]:
                chip = chip.strip()
                if chip:
                    tk.Label(
                        keywords_frame,
                        text=chip,
                        bg=THEME["accent_soft"],
                        fg=THEME["accent2"],
                        font=THEME["font_small"],
                        padx=7,
                        pady=2,
                        relief="flat",
                    ).pack(side="left", padx=2)

        # Abstract is collapsed by default to keep the list scannable; the full
        # text (not a truncated snippet) expands on demand.
        self._abstract_expanded = False
        self._abstract_toggle = None
        self._abstract_body = None
        abstract = (result.get("abstract") or "").strip()
        if abstract:
            self._abstract_toggle = tk.Label(
                self,
                text="▸ Show abstract",
                bg=THEME["card"],
                fg=THEME["accent2"],
                font=THEME["font_small"],
                cursor="hand2",
                anchor="w",
            )
            self._abstract_toggle.pack(fill="x", padx=12, pady=(0, 6))
            self._abstract_toggle.bind("<Button-1>", lambda _e: self._toggle_abstract(), add="+")
            self._abstract_body = tk.Label(
                self,
                text=abstract,
                bg=THEME["card"],
                fg=THEME["subtext"],
                font=THEME["font_small"],
                wraplength=720,
                justify="left",
                anchor="w",
            )  # packed only when expanded

        footer = tk.Frame(self, bg=THEME["card"])
        footer.pack(fill="x", padx=12, pady=(0, 10))
        self._footer = footer

        path_label = tk.Label(
            footer,
            text=result["path"],
            bg=THEME["card"],
            fg=THEME["subtext"],
            font=THEME["font_mono"],
            anchor="w",
            cursor="hand2",
        )
        path_label.pack(side="left", fill="x", expand=True)
        path_label.bind("<Button-1>", lambda _event: self.on_open(result["path"]), add="+")

        if result.get("doi"):
            tk.Label(
                footer,
                text=f"DOI: {result['doi'][:40]}",
                bg=THEME["card"],
                fg=THEME["accent2"],
                font=THEME["font_mono"],
            ).pack(side="right", padx=4)

        for widget in self.winfo_children():
            widget.bind("<Enter>", self._hover_on, add="+")
            widget.bind("<Leave>", self._hover_off, add="+")

    def _toggle_abstract(self) -> None:
        if not self._abstract_body:
            return
        self._abstract_expanded = not self._abstract_expanded
        if self._abstract_expanded:
            self._abstract_body.configure(bg=self.cget("bg"))  # match current hover state
            self._abstract_body.pack(fill="x", padx=12, pady=(0, 8), before=self._footer)
            self._abstract_toggle.configure(text="▾ Hide abstract")
        else:
            self._abstract_body.pack_forget()
            self._abstract_toggle.configure(text="▸ Show abstract")

    def _set_background_recursive(self, widget: tk.Widget, color: str) -> None:
        with suppress(tk.TclError):
            current_bg = widget.cget("bg")
            if current_bg in {THEME["card"], THEME["card_hover"]}:
                widget.configure(bg=color)
        for child in widget.winfo_children():
            self._set_background_recursive(child, color)

    def _hover_on(self, _event=None) -> None:
        self.configure(bg=THEME["card_hover"], highlightbackground=THEME["accent"])
        self._set_background_recursive(self, THEME["card_hover"])

    def _hover_off(self, _event=None) -> None:
        self.configure(bg=THEME["card"], highlightbackground=THEME["border"])
        self._set_background_recursive(self, THEME["card"])


class CollectDialog(tk.Toplevel):
    """Copy papers that exactly contain ALL keywords from a source to a folder."""

    def __init__(self, parent: "App", source_default: str):
        super().__init__(parent)
        self.title("Collect exact matches")
        self.configure(bg=THEME["bg"], padx=16, pady=14)
        self.resizable(False, False)
        self.transient(parent)

        self._running = False
        self._cancelled = False
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._source_var = tk.StringVar(value=source_default)
        self._keywords_var = tk.StringVar()
        self._dest_var = tk.StringVar()
        self._match_case_var = tk.BooleanVar(value=False)

        self._build()
        self.bind("<Escape>", lambda _e: self._on_close(), add="+")

    # UI ----------------------------------------------------------------------

    def _row_label(self, text: str, row: int) -> None:
        tk.Label(
            self, text=text, bg=THEME["bg"], fg=THEME["subtext"],
            font=THEME["font_small"], anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=(6, 0))

    def _entry(self, var: tk.StringVar, width: int = 46) -> tk.Entry:
        return tk.Entry(
            self, textvariable=var, width=width, bg=THEME["entry_bg"], fg=THEME["text"],
            insertbackground=THEME["text"], font=THEME["font_main"], bd=0,
            highlightthickness=1, highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )

    def _folder_button(self, var: tk.StringVar, title: str, row: int) -> None:
        tk.Button(
            self, text="Browse", bg=THEME["surface"], fg=THEME["subtext"],
            font=THEME["font_small"], bd=0, padx=10, pady=4, cursor="hand2",
            activebackground=THEME["accent_soft"], activeforeground=THEME["text"],
            command=lambda: self._pick_folder(var, title),
        ).grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(2, 0))

    def _build(self) -> None:
        tk.Label(
            self, text="Copy papers that exactly contain ALL keywords into a folder.",
            bg=THEME["bg"], fg=THEME["text"], font=THEME["font_title"], anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._row_label("Source folder", 1)
        self._entry(self._source_var).grid(row=1, column=1, sticky="we", padx=(8, 0), pady=(2, 0), ipady=4)
        self._folder_button(self._source_var, "Select source folder", 1)

        self._row_label("Keywords (comma-separated, ALL must match)", 2)
        kw_entry = self._entry(self._keywords_var)
        kw_entry.grid(row=2, column=1, sticky="we", padx=(8, 0), pady=(2, 0), ipady=4)

        self._row_label("Destination folder", 3)
        self._entry(self._dest_var).grid(row=3, column=1, sticky="we", padx=(8, 0), pady=(2, 0), ipady=4)
        self._folder_button(self._dest_var, "Select destination folder", 3)

        tk.Checkbutton(
            self, text="Match case", variable=self._match_case_var, bg=THEME["bg"],
            fg=THEME["subtext"], font=THEME["font_small"], activebackground=THEME["bg"],
            selectcolor=THEME["entry_bg"], anchor="w",
        ).grid(row=4, column=1, sticky="w", pady=(8, 0))

        self._status = tk.Label(
            self, text="", bg=THEME["bg"], fg=THEME["subtext"],
            font=THEME["font_small"], anchor="w", wraplength=560, justify="left",
        )
        self._status.grid(row=5, column=0, columnspan=3, sticky="we", pady=(10, 0))

        button_row = tk.Frame(self, bg=THEME["bg"])
        button_row.grid(row=6, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self._collect_btn = tk.Button(
            button_row, text="Collect", bg=THEME["accent"], fg="white",
            font=THEME["font_small"], bd=0, padx=16, pady=6, cursor="hand2",
            activebackground="#B85E31", activeforeground="white", command=self._start,
        )
        self._collect_btn.pack(side="right")
        self._close_btn = tk.Button(
            button_row, text="Close", bg=THEME["surface"], fg=THEME["subtext"],
            font=THEME["font_small"], bd=0, padx=14, pady=6, cursor="hand2",
            activebackground=THEME["accent_soft"], activeforeground=THEME["text"],
            command=self._on_close,
        )
        self._close_btn.pack(side="right", padx=(0, 8))

        self.columnconfigure(1, weight=1)

    def _pick_folder(self, var: tk.StringVar, title: str) -> None:
        path = filedialog.askdirectory(title=title, parent=self)
        if path:
            var.set(path)

    # Run ---------------------------------------------------------------------

    def _set_status(self, message: str, color: Optional[str] = None) -> None:
        self._status.configure(text=message, fg=color or THEME["subtext"])

    def _ui(self, callback, *args) -> None:
        if self._closing:
            return
        with suppress(tk.TclError):
            self.after(0, callback, *args)

    def _start(self) -> None:
        if self._running:
            return
        source = self._source_var.get().strip()
        dest = self._dest_var.get().strip()
        keywords = parse_keywords(self._keywords_var.get())

        if not os.path.isdir(source):
            messagebox.showerror("Collect", f"Source folder not found:\n{source}", parent=self)
            return
        if not keywords:
            messagebox.showerror("Collect", "Enter at least one keyword.", parent=self)
            return
        if not dest:
            messagebox.showerror("Collect", "Choose a destination folder.", parent=self)
            return
        if os.path.abspath(dest) == os.path.abspath(source):
            messagebox.showerror("Collect", "Destination must differ from the source.", parent=self)
            return

        self._running = True
        self._cancelled = False
        self._collect_btn.configure(state="disabled", text="Collecting…")
        self._set_status("Starting…", color=THEME["accent2"])

        worker = threading.Thread(
            target=self._run,
            args=(source, dest, keywords, self._match_case_var.get()),
            daemon=True,
        )
        worker.start()

    def _run(self, source: str, dest: str, keywords: list[str], match_case: bool) -> None:
        def progress(phase: str, done: int, total: int, name: str) -> None:
            if phase == "index":
                self._ui(self._set_status, f"Indexing {done}/{total}: {name[:48]}")
            elif phase == "dest":
                self._ui(self._set_status, f"Checking destination for duplicates {done}/{total}…")
            else:
                self._ui(self._set_status, f"Scanning matches {done}/{total}…")

        try:
            stats = aggregate_matches(
                source, dest, keywords,
                match_case=match_case,
                progress=progress,
                should_cancel=lambda: self._cancelled or self._closing,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("Collect failed")
            self._ui(self._finish_error, str(exc))
            return
        self._ui(self._finish_ok, stats)

    def _finish_error(self, message: str) -> None:
        self._running = False
        self._collect_btn.configure(state="normal", text="Collect")
        self._set_status(f"Failed: {message}", color=THEME["red"])

    def _finish_ok(self, stats: AggregateStats) -> None:
        self._running = False
        self._collect_btn.configure(state="normal", text="Collect")
        if stats.cancelled:
            self._set_status("Cancelled.", color=THEME["highlight"])
            return

        lines: list[str] = []
        if stats.copied:
            lines.append(f"Copied {stats.copied} paper(s) to the destination.")
        if stats.matched == 0:
            lines.append("No papers exactly matched all keywords.")
        if stats.skipped_existing:
            lines.append(f"{stats.skipped_existing} already in the folder — skipped (not re-copied).")
        if stats.skipped_nonpaper:
            lines.append(f"{stats.skipped_nonpaper} matched but were discarded as non-papers.")
        if stats.unreadable:
            lines.append(
                f"{stats.unreadable} PDF(s) have no extractable text (scanned or damaged) "
                "and could not be checked — OCR them to include them."
            )
        if stats.possible_duplicates:
            shown = "\n  ".join(stats.possible_duplicates[:5])
            more = len(stats.possible_duplicates) - 5
            lines.append(
                f"{len(stats.possible_duplicates)} copied paper(s) share a DOI/arXiv id with one "
                f"already there — possibly another version of the same paper; please check:\n  {shown}"
                + (f"\n  …and {more} more" if more > 0 else "")
            )
        if stats.errors:
            lines.append(f"{stats.errors} error(s) — see logs.")

        color = (
            THEME["highlight"] if stats.possible_duplicates
            else THEME["green"] if stats.copied
            else THEME["highlight"] if (stats.skipped_existing or stats.skipped_nonpaper)
            else THEME["subtext"]
        )
        self._set_status("  ".join(lines) or "Done.", color=color)

        detail = "\n".join(lines) + (
            f"\n\nScanned {stats.total_indexed} document(s); "
            f"{stats.candidates} keyword candidate(s)."
        )
        if stats.copied:
            if messagebox.askyesno(
                "Collect complete", detail + "\n\nOpen the destination folder?", parent=self
            ):
                with suppress(Exception):
                    if sys.platform == "win32":
                        os.startfile(stats.dest_dir)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", stats.dest_dir])
                    else:
                        subprocess.Popen(["xdg-open", stats.dest_dir])
        else:
            messagebox.showinfo("Collect complete", detail, parent=self)

    def _on_close(self) -> None:
        if self._running:
            self._cancelled = True
            self._set_status("Cancelling…", color=THEME["highlight"])
            return
        self._closing = True
        self.destroy()


class HiddenDocsDialog(tk.Toplevel):
    """
    Lists PDFs that search and Collect leave out (not judged a paper/report, or
    no extractable text), with the reason, and lets the user override the
    classifier per document. Overrides survive re-indexing.
    """

    STATUS_AUTO = "Hidden: not a paper/report"
    STATUS_TEXTLESS = "No text (scanned/damaged)"
    STATUS_INCLUDED = "Included by you"
    STATUS_HIDDEN = "Hidden by you"

    def __init__(self, parent: "App", db_path: str, on_change: Callable[[], None]):
        super().__init__(parent)
        self.title("Hidden documents")
        self.configure(bg=THEME["bg"], padx=14, pady=12)
        self.geometry("900x480")
        self.minsize(640, 320)
        self.transient(parent)
        self._conn = open_db(db_path)
        self._on_change = on_change
        self._rows: dict[str, sqlite3.Row] = {}
        self._build()
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close(), add="+")

    def _build(self) -> None:
        tk.Label(
            self,
            text="These PDFs are left out of search and Collect. If one is a genuine paper "
                 "or report, select it and choose Include.",
            bg=THEME["bg"], fg=THEME["subtext"], font=THEME["font_small"],
            anchor="w", justify="left", wraplength=860,
        ).pack(fill="x", pady=(0, 8))

        frame = tk.Frame(self, bg=THEME["bg"])
        frame.pack(fill="both", expand=True)
        self._tree = ttk.Treeview(frame, columns=("status", "file", "reason"), show="headings",
                                  selectmode="extended")
        for column, label, width in (("status", "Status", 190), ("file", "File", 330), ("reason", "Why", 330)):
            self._tree.heading(column, text=label)
            self._tree.column(column, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)
        self._tree.bind("<Double-1>", lambda _e: self._open_selected(), add="+")

        buttons = tk.Frame(self, bg=THEME["bg"])
        buttons.pack(fill="x", pady=(10, 0))
        self._count = tk.Label(buttons, text="", bg=THEME["bg"], fg=THEME["subtext"], font=THEME["font_small"])
        self._count.pack(side="left")
        for text, command, primary in (("Close", self._close, False),
                                       ("Hide", lambda: self._set_selected(False), False),
                                       ("Include in search", lambda: self._set_selected(True), True),
                                       ("Open PDF", self._open_selected, False)):
            tk.Button(
                buttons, text=text, command=command, bd=0, padx=12, pady=5, cursor="hand2",
                font=THEME["font_small"],
                bg=THEME["accent"] if primary else THEME["surface"],
                fg="white" if primary else THEME["subtext"],
                activebackground="#B85E31" if primary else THEME["accent_soft"],
            ).pack(side="right", padx=(6, 0))

    def _status(self, row) -> str:
        if not row["has_text"]:
            return self.STATUS_TEXTLESS
        if row["override"] is not None:
            return self.STATUS_INCLUDED if row["visible"] else self.STATUS_HIDDEN
        return self.STATUS_AUTO

    def _refresh(self) -> None:
        rows = self._conn.execute(
            "SELECT p.path, p.filename, p.doc_reason, p.has_text, p.visible, o.visible AS override "
            "FROM papers p LEFT JOIN overrides o ON o.path = p.path "
            "WHERE p.visible = 0 OR o.path IS NOT NULL "
            "ORDER BY p.has_text DESC, p.visible, p.filename COLLATE NOCASE"
        ).fetchall()
        self._tree.delete(*self._tree.get_children())
        self._rows = {}
        for row in rows:
            self._rows[row["path"]] = row
            self._tree.insert("", "end", iid=row["path"],
                              values=(self._status(row), row["filename"], row["doc_reason"]))
        hidden = sum(1 for row in rows if not row["visible"])
        self._count.configure(text=f"{hidden} hidden · {len(rows) - hidden} included by you")

    def _selected(self) -> list:
        return [self._rows[iid] for iid in self._tree.selection() if iid in self._rows]

    def _set_selected(self, visible: bool) -> None:
        rows = self._selected()
        if not rows:
            return
        textless = [row for row in rows if not row["has_text"]]
        if visible and textless:
            messagebox.showinfo(
                "No text",
                f"{len(textless)} selected PDF(s) have no extractable text (scanned or damaged), "
                "so they cannot be searched. Run OCR on them first.",
                parent=self,
            )
        changed = False
        for row in rows:
            if row["has_text"] and bool(row["visible"]) != visible:
                set_visibility(self._conn, row["path"], visible)
                changed = True
        if changed:
            self._refresh()
            self._on_change()

    def _open_selected(self) -> None:
        for row in self._selected()[:5]:
            with suppress(Exception):
                if sys.platform == "win32":
                    os.startfile(row["path"])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", row["path"]])
                else:
                    subprocess.Popen(["xdg-open", row["path"]])

    def _close(self) -> None:
        with suppress(sqlite3.Error):
            self._conn.close()
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Research Paper Search")
        self.geometry("980x720")
        self.minsize(760, 520)
        self.configure(bg=THEME["bg"])
        self._set_window_icon()

        self._directory = ""
        self._db_path = ""
        self._search_conn: Optional[sqlite3.Connection] = None
        self._indexing = False
        self._index_job_id = 0
        self._active_job_id = 0
        self._pending_index_request: Optional[tuple[str, str, bool, int]] = None
        self._last_query = ""
        self._search_after_id = None
        self._render_token = 0
        self._render_after_id = None
        self._closing = False
        # Searches run on a background thread so the UI never waits on SQLite.
        self._search_seq = 0
        self._searcher = SearchWorker(
            lambda seq, results, error: self._safe_after(self._on_search_result, seq, results, error)
        )
        self._searcher.start()

        self._setup_styles()
        self._build_ui()
        self._check_deps()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self) -> None:
        """Apply the embedded icon to the window/taskbar; ignore if unsupported."""
        with suppress(Exception):
            self._icon_image = tk.PhotoImage(data="".join(APP_ICON_B64.split()))
            self.iconphoto(True, self._icon_image)  # True => default for all toplevels

    # Styling -----------------------------------------------------------------

    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "TProgressbar",
            troughcolor=THEME["bar_bg"],
            background=THEME["accent"],
            lightcolor=THEME["accent"],
            darkcolor=THEME["accent"],
            bordercolor=THEME["bar_bg"],
            thickness=5,
        )
        style.configure(
            "TScrollbar",
            background=THEME["surface"],
            troughcolor=THEME["bg"],
            arrowcolor=THEME["subtext"],
            borderwidth=0,
            relief="flat",
        )

    # UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=THEME["surface"], pady=12)
        header.pack(fill="x")

        tk.Label(
            header,
            text="Research Paper Search",
            bg=THEME["surface"],
            fg=THEME["text"],
            font=THEME["font_head"],
        ).pack(side="left", padx=18)

        self._status_var = tk.StringVar(value="No directory selected")
        self._status_label = tk.Label(
            header,
            textvariable=self._status_var,
            bg=THEME["surface"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
        )
        self._status_label.pack(side="right", padx=18)

        dir_bar = tk.Frame(self, bg=THEME["surface"], pady=8)
        dir_bar.pack(fill="x", pady=(1, 0))

        tk.Label(
            dir_bar,
            text="Folder",
            bg=THEME["surface"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
        ).pack(side="left", padx=(16, 6))

        self._dir_var = tk.StringVar()
        dir_entry = tk.Entry(
            dir_bar,
            textvariable=self._dir_var,
            bg=THEME["entry_bg"],
            fg=THEME["text"],
            insertbackground=THEME["text"],
            selectbackground=THEME["accent_soft"],
            selectforeground=THEME["text"],
            font=THEME["font_main"],
            bd=0,
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )
        dir_entry.pack(side="left", fill="x", expand=True, padx=4, ipady=6)
        dir_entry.bind("<Return>", lambda _event: self._select_directory_text(), add="+")

        tk.Button(
            dir_bar,
            text="Browse",
            bg=THEME["accent"],
            fg="white",
            font=THEME["font_small"],
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            activebackground="#B85E31",
            activeforeground="white",
            command=self._browse_directory,
        ).pack(side="left", padx=4)

        tk.Button(
            dir_bar,
            text="Re-index",
            bg=THEME["surface"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
            bd=0,
            padx=10,
            pady=6,
            cursor="hand2",
            activebackground=THEME["accent_soft"],
            activeforeground=THEME["text"],
            command=self._reindex,
        ).pack(side="left", padx=(0, 6))

        collect_btn = tk.Button(
            dir_bar,
            text="Collect exact matches…",
            bg=THEME["accent2"],
            fg="white",
            font=THEME["font_small"],
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            activebackground="#7A4F37",
            activeforeground="white",
            command=self._open_collect_dialog,
        )
        collect_btn.pack(side="left", padx=(0, 6))
        Tooltip(collect_btn, "Copy papers that exactly contain ALL keywords to a folder")

        hidden_btn = tk.Button(
            dir_bar,
            text="Hidden…",
            bg=THEME["surface"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
            bd=0,
            padx=10,
            pady=6,
            cursor="hand2",
            activebackground=THEME["accent_soft"],
            activeforeground=THEME["text"],
            command=self._open_hidden_dialog,
        )
        hidden_btn.pack(side="left", padx=(0, 14))
        Tooltip(hidden_btn, "PDFs not shown in results (non-papers, scanned files) — review and include")

        self._progress = ttk.Progressbar(self, mode="indeterminate", style="TProgressbar")

        search_frame = tk.Frame(self, bg=THEME["bg"], pady=10)
        search_frame.pack(fill="x", padx=18)

        tk.Label(
            search_frame,
            text="Search",
            bg=THEME["bg"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
        ).pack(side="left", padx=(0, 8))

        self._query_var = tk.StringVar()
        self._query_var.trace_add("write", self._on_query_change)

        self._search_entry = tk.Entry(
            search_frame,
            textvariable=self._query_var,
            bg=THEME["entry_bg"],
            fg=THEME["text"],
            insertbackground=THEME["text"],
            selectbackground=THEME["accent_soft"],
            selectforeground=THEME["text"],
            font=("Segoe UI", 13),
            bd=0,
            highlightthickness=2,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )
        self._search_entry.pack(side="left", fill="x", expand=True, ipady=8)
        self._search_entry.bind("<Escape>", lambda _event: self._query_var.set(""), add="+")

        tk.Label(
            search_frame,
            text="Years",
            bg=THEME["bg"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
        ).pack(side="left", padx=(14, 6))

        self._year_var = tk.StringVar()
        self._year_var.trace_add("write", self._on_query_change)
        year_entry = tk.Entry(
            search_frame,
            textvariable=self._year_var,
            bg=THEME["entry_bg"],
            fg=THEME["text"],
            insertbackground=THEME["text"],
            selectbackground=THEME["accent_soft"],
            selectforeground=THEME["text"],
            font=THEME["font_main"],
            bd=0,
            width=11,
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )
        year_entry.pack(side="left", ipady=6, padx=(0, 8))
        year_entry.bind("<Return>", lambda _event: self._do_search(), add="+")
        Tooltip(year_entry, "Filter by year or range, e.g. 2024, 2020-2025, 2020- or -2019")

        self._results_label = tk.Label(
            self,
            text="",
            bg=THEME["bg"],
            fg=THEME["subtext"],
            font=THEME["font_small"],
            anchor="w",
        )
        self._results_label.pack(fill="x", padx=22, pady=(0, 4))

        self._scroll = ScrollableFrame(self)
        self._scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._cards_frame = self._scroll.inner

        self._placeholder = tk.Label(
            self._cards_frame,
            text=(
                "Select a directory and type your search query above.\n\n"
                "Search by paper title, topic, author, keywords, or any concept\n"
                "found in the abstract or body of the paper."
            ),
            bg=THEME["bg"],
            fg=THEME["subtext"],
            font=("Segoe UI", 11),
            justify="center",
        )
        self._placeholder.pack(expand=True, pady=70)

        self._search_entry.focus_set()

    # Lifecycle ----------------------------------------------------------------

    def _safe_after(self, callback, *args) -> None:
        if self._closing:
            return
        with suppress(tk.TclError):
            self.after(0, callback, *args)

    def _on_close(self) -> None:
        self._closing = True
        self._index_job_id += 1
        self._searcher.stop()
        self._cancel_render()
        if self._search_after_id:
            with suppress(tk.TclError):
                self.after_cancel(self._search_after_id)
        if self._search_conn:
            with suppress(sqlite3.Error):
                self._search_conn.close()
            self._search_conn = None
        self.destroy()

    # Dependency checks --------------------------------------------------------

    def _check_deps(self) -> None:
        missing = []
        if not PDFIUM_OK:
            missing.append("pypdfium2")
        if not RAPIDFUZZ_OK:
            missing.append("rapidfuzz")
        if missing:
            self._set_status(
                "Optional packages not found. Install for best performance: "
                + " ".join(missing),
                color=THEME["highlight"],
            )

    # Directory handling -------------------------------------------------------

    def _browse_directory(self) -> None:
        path = filedialog.askdirectory(title="Select directory containing PDFs")
        if path:
            self._dir_var.set(path)
            self._load_directory(path)

    def _select_directory_text(self) -> None:
        path = self._dir_var.get().strip()
        if os.path.isdir(path):
            self._load_directory(path)
            return
        messagebox.showerror("Error", f"Directory not found:\n{path}")

    def _load_directory(self, path: str) -> None:
        db_path = get_db_path(path)
        try:
            search_conn = open_db(db_path)
        except sqlite3.Error as exc:
            messagebox.showerror("Database Error", f"Could not open the search index:\n{exc}")
            self._set_status("Failed to open index database.", color=THEME["red"])
            return

        old_conn = self._search_conn
        self._search_conn = search_conn
        self._directory = path
        self._db_path = db_path
        self._dir_var.set(path)

        if old_conn:
            with suppress(sqlite3.Error):
                old_conn.close()

        self._set_status(f"Loaded: {path}")
        self._start_indexing(path, db_path, force_reindex=False)

    def _reindex(self) -> None:
        if not self._directory or not self._db_path:
            messagebox.showinfo("No directory", "Please select a directory first.")
            return
        self._start_indexing(self._directory, self._db_path, force_reindex=True)

    def _open_hidden_dialog(self) -> None:
        if not self._db_path:
            messagebox.showinfo("No directory", "Please select a directory first.")
            return
        dialog = HiddenDocsDialog(self, self._db_path, on_change=self._on_visibility_change)
        dialog.focus_set()

    def _on_visibility_change(self) -> None:
        if self._last_query:
            self._do_search()

    def _hide_result(self, path: str) -> None:
        if not self._search_conn:
            return
        try:
            set_visibility(self._search_conn, path, False)
        except sqlite3.Error as exc:
            messagebox.showerror("Hide", f"Could not hide the document:\n{exc}")
            return
        self._set_status(f"Hidden: {os.path.basename(path)} — undo under Hidden…", color=THEME["highlight"])
        self._on_visibility_change()

    def _open_collect_dialog(self) -> None:
        dialog = CollectDialog(self, source_default=self._directory)
        dialog.focus_set()

    # Indexing -----------------------------------------------------------------

    def _start_indexing(self, directory: str, db_path: str, force_reindex: bool) -> None:
        self._index_job_id += 1
        job_id = self._index_job_id

        if self._indexing:
            self._pending_index_request = (directory, db_path, force_reindex, job_id)
            self._set_status("Restarting scan with the latest directory changes...")
            return

        self._launch_index_job(directory, db_path, force_reindex, job_id)

    def _launch_index_job(self, directory: str, db_path: str, force_reindex: bool, job_id: int) -> None:
        self._indexing = True
        self._active_job_id = job_id
        self._pending_index_request = None
        self._progress.pack(fill="x", padx=18, pady=0, before=self._scroll)
        self._progress.start(12)

        worker = threading.Thread(
            target=self._index_worker,
            args=(directory, db_path, force_reindex, job_id),
            daemon=True,
        )
        worker.start()

    def _index_worker(self, directory: str, db_path: str, force_reindex: bool, job_id: int) -> None:
        def progress(done: int, total: int, name: str) -> None:
            self._safe_after(self._set_status, f"Indexing {done}/{total}: {name[:70]}")

        def should_cancel() -> bool:
            return job_id != self._index_job_id or self._closing

        try:
            stats = index_directory(
                directory,
                db_path,
                force_reindex=force_reindex,
                progress=progress,
                should_cancel=should_cancel,
            )
        except Exception as exc:
            LOGGER.exception("Index worker failed")
            stats = IndexStats(0, 0, 0, 1, False, [f"Index worker error: {exc}"])

        self._safe_after(self._indexing_done, job_id, stats)

    def _indexing_done(self, job_id: int, stats: IndexStats) -> None:
        if job_id != self._active_job_id and not stats.cancelled:
            return

        self._indexing = False
        self._progress.stop()
        self._progress.pack_forget()

        if stats.cancelled:
            self._set_status("Indexing restarted with newer settings...")
        else:
            message = f"{stats.total} PDFs total, {stats.indexed} indexed, {stats.skipped} cached"
            if stats.errors:
                message += f", {stats.errors} errors"
            discarded = textless = 0
            if self._search_conn:
                textless = count_textless(self._search_conn)
                if REQUIRE_SCHOLARLY:
                    discarded = count_nonscholarly(self._search_conn)
            notes = []
            if discarded:
                notes.append(f"{discarded} non-paper doc(s) hidden (see Hidden…)")
            if textless:
                notes.append(f"{textless} without text (scanned/damaged)")
            if notes:
                message += " — " + ", ".join(notes)
            has_note = bool(stats.errors or notes)
            self._set_status(message, color=THEME["highlight"] if has_note else THEME["subtext"])
            if stats.errors and stats.error_samples:
                details = "\n".join(stats.error_samples)
                LOGGER.warning("Indexing completed with errors:\n%s", details)

        if self._pending_index_request:
            directory, db_path, force_reindex, pending_job_id = self._pending_index_request
            self._launch_index_job(directory, db_path, force_reindex, pending_job_id)
            return

        if self._last_query:
            self._do_search()

    # Search ------------------------------------------------------------------

    def _on_query_change(self, *_args) -> None:
        if self._search_after_id:
            with suppress(tk.TclError):
                self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(250, self._do_search)

    def _do_search(self) -> None:
        query = self._query_var.get().strip()
        year = self._year_var.get().strip() or None

        self._search_seq += 1  # any result still in flight is now stale
        if not query:
            self._last_query = ""
            self._clear_results()
            return

        self._last_query = query

        if not self._db_path:
            self._set_status("Please select a directory first.", color=THEME["highlight"])
            return

        self._searcher.submit(self._db_path, query, year, self._search_seq)

    def _on_search_result(self, seq: int, results: Optional[list], error: Optional[Exception]) -> None:
        if seq != self._search_seq or self._closing:
            return  # superseded by a newer query
        if error is not None:
            self._set_status("Search failed. Please try reloading the directory.", color=THEME["red"])
            LOGGER.error("Search failed: %s", error)
            return
        self._render_results(results)

    # Results -----------------------------------------------------------------

    def _cancel_render(self) -> None:
        """Invalidate any in-flight incremental render."""
        self._render_token += 1
        if self._render_after_id:
            with suppress(tk.TclError):
                self.after_cancel(self._render_after_id)
            self._render_after_id = None

    def _clear_results(self) -> None:
        self._cancel_render()
        for widget in self._cards_frame.winfo_children():
            widget.destroy()
        self._placeholder = tk.Label(
            self._cards_frame,
            text="Type a query to search your papers.",
            bg=THEME["bg"],
            fg=THEME["subtext"],
            font=("Segoe UI", 11),
            justify="center",
        )
        self._placeholder.pack(expand=True, pady=70)
        self._results_label.config(text="")

    def _render_results(self, results: list[dict]) -> None:
        self._cancel_render()
        for widget in self._cards_frame.winfo_children():
            widget.destroy()

        if not results:
            tk.Label(
                self._cards_frame,
                text="No results found.\n\nTry different keywords or check the directory.",
                bg=THEME["bg"],
                fg=THEME["subtext"],
                font=("Segoe UI", 11),
                justify="center",
            ).pack(expand=True, pady=70)
            self._results_label.config(text="No results")
            return

        self._results_label.config(
            text=f"  {len(results)} result{'s' if len(results) != 1 else ''} found"
        )
        # Build cards a few at a time, yielding to the event loop between batches,
        # so the search box stays responsive while typing (Tk is single-threaded).
        self._render_batch(self._render_token, results, 0)

    def _render_batch(self, token: int, results: list[dict], start: int) -> None:
        if token != self._render_token or self._closing:
            return
        end = min(start + RENDER_BATCH_SIZE, len(results))
        for index in range(start, end):
            card = ResultCard(
                self._cards_frame,
                results[index],
                on_open=self._open_pdf,
                on_reveal=self._reveal_in_explorer,
                on_hide=self._hide_result,
            )
            card.pack(fill="x", padx=8, pady=4)
        if end < len(results):
            self._render_after_id = self.after(1, self._render_batch, token, results, end)
        else:
            self._render_after_id = None

    # Actions -----------------------------------------------------------------

    def _open_pdf(self, path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("Error", f"Could not open file:\n{exc}")

    def _reveal_in_explorer(self, path: str) -> None:
        try:
            folder = os.path.dirname(path)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("Error", f"Could not reveal file:\n{exc}")

    # Helpers -----------------------------------------------------------------

    def _set_status(self, message: str, color: Optional[str] = None) -> None:
        self._status_var.set(message)
        self._status_label.configure(fg=color or THEME["subtext"])


# -----------------------------------------------------------------------------
# ENTRY POINTS
# -----------------------------------------------------------------------------

def _run_headless_index(directory: str, force: bool, workers: Optional[int]) -> int:
    """Build the index for a directory without launching the UI (12-factor XII)."""
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}", file=sys.stderr)
        return 2

    def progress(done: int, total: int, name: str) -> None:
        print(f"\rIndexing {done}/{total}: {name[:60]:<60}", end="", file=sys.stderr, flush=True)

    stats = index_directory(
        directory,
        get_db_path(directory),
        force_reindex=force,
        workers=workers,
        progress=progress,
    )
    print(file=sys.stderr)
    print(
        f"{stats.total} PDFs total, {stats.indexed} indexed, "
        f"{stats.skipped} cached, {stats.errors} errors"
    )
    with suppress(sqlite3.Error):
        conn = open_db(get_db_path(directory))
        try:
            discarded = count_nonscholarly(conn) if REQUIRE_SCHOLARLY else 0
            textless = count_textless(conn)
        finally:
            conn.close()
        if discarded:
            print(f"  {discarded} document(s) excluded as non-papers.")
        if textless:
            print(f"  {textless} PDF(s) have no extractable text (scanned/damaged).")
    for sample in stats.error_samples:
        print(f"  ! {sample}", file=sys.stderr)
    return 1 if stats.errors else 0


def _run_headless_collect(
    source: str, dest: str, raw_keywords: str, match_case: bool, workers: Optional[int]
) -> int:
    """Collect exactly-matching papers from source into dest, headlessly."""
    source = os.path.abspath(source)
    if not os.path.isdir(source):
        print(f"Not a directory: {source}", file=sys.stderr)
        return 2
    keywords = parse_keywords(raw_keywords)
    if not keywords:
        print("No keywords provided (use --keywords).", file=sys.stderr)
        return 2

    def progress(phase: str, done: int, total: int, name: str) -> None:
        label = {"index": "Indexing", "dest": "Checking destination"}.get(phase, "Scanning")
        print(f"\r{label} {done}/{total} {name[:48]:<48}", end="", file=sys.stderr, flush=True)

    stats = aggregate_matches(
        source, dest, keywords, match_case=match_case, workers=workers, progress=progress,
    )
    print(file=sys.stderr)
    print(
        f"{stats.total_indexed} documents scanned, {stats.matched} exact match(es), "
        f"{stats.copied} copied to {stats.dest_dir}"
    )
    if stats.skipped_existing:
        print(f"  {stats.skipped_existing} already present in destination - skipped.")
    if stats.skipped_nonpaper:
        print(f"  {stats.skipped_nonpaper} matched but discarded as non-papers.")
    if stats.unreadable:
        print(f"  {stats.unreadable} PDF(s) have no extractable text (scanned/damaged) - not checked.")
    if stats.possible_duplicates:
        print(f"  {len(stats.possible_duplicates)} copied paper(s) share a DOI/arXiv id with one already there:")
        for note in stats.possible_duplicates:
            print(f"    {note}")
    if stats.errors:
        print(f"  {stats.errors} error(s).")
    return 1 if stats.errors else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="research_paper_search",
        description="Index and search research-paper PDFs by content.",
    )
    parser.add_argument(
        "--index",
        metavar="DIR",
        help="Index the PDFs under DIR headlessly, then exit (no UI).",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Copy papers exactly matching ALL --keywords from --index DIR into --dest.",
    )
    parser.add_argument(
        "--dest",
        metavar="DIR",
        help="Destination folder for --collect.",
    )
    parser.add_argument(
        "--keywords",
        metavar='"a,b,c"',
        help="Comma-separated keywords for --collect; a paper must contain them ALL exactly.",
    )
    parser.add_argument(
        "--match-case",
        action="store_true",
        help="Make --collect keyword matching case-sensitive.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract every file, ignoring the incremental cache.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Extraction worker processes (0 = auto-detect CPU count).",
    )
    args = parser.parse_args(argv)

    if args.collect:
        if not args.index or not args.dest:
            parser.error("--collect requires --index SOURCE_DIR and --dest DEST_DIR")
        return _run_headless_collect(
            args.index, args.dest, args.keywords or "", args.match_case, args.workers
        )

    if args.index:
        return _run_headless_index(args.index, args.force, args.workers)

    App().mainloop()
    return 0


# -----------------------------------------------------------------------------
# APP ICON — 128x128 PNG (base64), embedded so the tool stays a single file.
# -----------------------------------------------------------------------------
APP_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAMLElEQVR42u2d+VcUVxaA+0/ov2BsNSCKbAq4AAZxHEiI"
    "2MQNkaU10aC4gDMqxiWdcYtDtA2axSTa0ahxx7iAy0irUcS1x3E8x4lJSJwTM1mcTjxqcMud+1q60zTdXdurqlfNu+d8"
    "5/CLj6p7v7fUq1doMvHgwYMHDx48ePDgwYMHDx48eKgYl6uGmBErYkeciCsAiHLcAfda354DG2KJ9qJbEEd7AoATktb2"
    "DpEdNYW/NGuIDXEhwJFEK1KFmA1a+Cxr+03wYirDg7m0G6nwFgR7fBZwqIKdKSub9eLjcJ/l4cVSFQeTxb84K8uJAEcT"
    "3Agba4OLM7PMiBsBjqZ4kGRefC6BfhJcwOIjwNEVD2LRofiZTp58ZsCOmGnWsvg2BDhM4dSm+DMyLYgHAQ5zWFUX4PyM"
    "TBcCHCbxIGY1i2/lSWYeh5oCtKp14VdeGwXf7F4Nd/59zgs8aTME925e817vf5u2wb/eKGdFAvpPBeenZ9oQoM2lubne"
    "5Bml4EIQGf6BMquRKwnQXxCem57pQoAml+bkentQtBTfx+O7P8HVFeVAO18S8JybnmGmWPwMCwI0uRilxe8sQYZe2GgK"
    "4KB9gd81bZU2tP70LRzf+R7sW78M3qkp14Wdby2Exs0O+PaLf4peH+gogJumAG6aF+dePEpS8a9fcOlW9HAQGcVc+4X1"
    "i+CzikG6SNBCYxpoqcwwI0CTr3etltTzWSu+DyKm0PW3thyBD0YnguuVgUA7jyKw0hDASvvCfpHwmEd6GqsCfLxytqh7"
    "eO/FBL0kcNAQwE5dgOviBdBzzheDWAEIm8Ylay2AS7EAZysznAjQRMr8TxZeLAvQdu9/gvfw6SKbX4KGielAO58RaKUg"
    "wGAXAjSRIgBZdbNa/A32qaLugQjwLhafsG18CtDOZySUCzANBZiGjVFEigDkkYtVAS4c2y1OgIW/C0Cgnc9IGF4AVheC"
    "ZGoSM/z7BShM8GMoAZqxEdrI2Vkjj1xk1c3CsE96vtjihxLg6MsDQI28hiE7KgQInhb0QErRo0eAqdgIZaJ1/z+8ADh6"
    "YOF9HH0pHdTIaxi4AHqzzytAXz+GEuDM1EFAGy5AOqiR1zBwAZgQwNrXzxEugLQEPmq7Azevn4erZw7A6f0fMoH7xB74"
    "/LIL2u7eFhZggZEFwFeZtJFafJJsVgofTEvjZrjr+U5QgLex8D6OTEoHNfIaBmUCnMZGaCNFANLzWS2+DzIySRVAjbyG"
    "wdgCsDTsR0KKAIcnpXEBxMLy8B8ImarC3UO9kQX4rGIg0EaKAGShxXrxLxzbHvEeQgmgRl7DoFAAPMVCGykCkFU2WWix"
    "LMDtWzcEBVg3sq+fwxPTQI28hsHYAhDIKpvFtQDp+ULF/12AeD+NXAD5myo///ANE0Sa86NKgFPYCG262k5g/atlnQRQ"
    "I69hUCjAFGyEMl1tJzCkACrkNQzGFiAadgL3ogBrsfA+GmypRhJgANCmq+0EegUoiPfzVIABWqFMgJPYCG262k5gKAHU"
    "yGsYjC1ANOwEGluAydgIZbraTmCwAIeIACrkNQzGFiAadgI7CVBuIAFOYCO06Wo7gXvml0FdQR8/RAA18hoG4wtg9J3A"
    "zgL0N5IA6UCbaNjd+/yaG3Z8tB7WLFsElaVj/JRbc/0/L62pgg1ra2HN5AKoG9HHz0GvAOlaoUwA18vpQBsj7ASG2ukj"
    "RScFHzVsEAyO+4Mknu3TDSak94S5ObFeAdTIaxiMLYDeO4FkAdrc1ODt0VKLHo785O6wqiCBC8D6TuCBTW9Caf4QaoUP"
    "JcLGMclcABZ3AtcsngHDUmJVK34gf8mJY1eAJjzDThvWdwJnlozQpPCBjOrfw7tDqEK+jS2AljuBx3asU3XIF5wSkrrD"
    "1qIU1gRIA9qwuhOoZ/F95PTtBg3lqTTzbWwBtNoJVDLsF+YMgqkloztQOjJX0XRwiJ4EygQ4jo3QhrWdwG3vOyQXiRS5"
    "fuc2+P7HH+Bu269hOdp4AGYWF0huv3JIL1r5VigAnmGnDUs7erdufgl/SusrqfDNp09GLHowrs1rYeOrr8Dk/CxJEqyz"
    "JtLINxcgElI2eN5dUyup8D6aUIDdy6u9rKwYB0MTe4peD5D3BroK8HdshDasFP9yyylRhSAjBBnuSTHvPWiDXx8+8HIf"
    "f5YqAOGThVPghfTeon73guG9leabC6C09/uK/+DRQ3jy228dePj4sVcKQQGWVfs5tWYe7LVXwLCkZ0SNAgfL+uspQCrQ"
    "hpW3eVKG/VDF9/EIJZAqwLl182Hr/EmirmFJXrySfCsUYCI2QhkWBCBv9cQs+HzDfrji+yBTglQBCK+X5Qtex4v4WKgg"
    "38oEOIaN0IYFAcS80vWt9klxhQQgI4RYAU4GCHBi1Z9FTQW7ilPk5psLIGf49/V+8QI8kibA2ho/YkaBJXl9uAC0ICd5"
    "xC78CPdFTAFtD6WMAHOhBQvvo3H5DMHrmTQ4RicB8A0VbYww/wfv8JHVfrjiP37yJOKTQNPmOtiFhfcRLABhTGaS4DpA"
    "Zr6VCXAUG6EN649/ZG8/uIikwI9CSECKf1/wMTBIAEdnAWZZswWllJlvpQL0B9qwLkDg/B8MWQ+QBR+Z88mwf0/EZpAY"
    "AV4vzRchgKx8cwFoCiCH41wAtgQgR7c1FWATCrC02s8JFOAsFj0QMQLsL+mnvQBH8GUEbbrcCBBKgLqaDogRQGa+uQBS"
    "BSCHOegLUOUnlAA144ZzAbSCfLEjlGytBSgdlhrxep7H84K6CHAYG6GN3gKQz7UEF1x4koemADux8D6CBWiqnS14PcX4"
    "VZHMfCsVoB/QxgjnAOxzZ6knwGoiwDw/ddPGCl7P/GFxcvOtUIAybIQyd1qv6i6BmGNgX371BRUB6mvndxDAhQI0Y+F9"
    "vJAmfDjkI/y/h2Xmmz0Bbl87a4h1AK1RILD4TwWY4y++mN5P5n8F+VYmQGNZPzcCNPm6cZPuApw8elDUYQyla4Fb//kq"
    "tABvzYNDS6eLehVcnd1Ldq5RALNSAVy0BTizcJxhzgSQqeLKFbdsAc437gopwPHaahgt8ALIxyfjkmTnWvH/HNpYigKU"
    "YmOUuX2tWXcBDu3ZJvrDDzkS/PyLB/bW1sDOJVUdaFhRBaU5qeKOoWfGKMozDQHq1RDgRPXz0HbnR0OMAr6RQOr3ACe3"
    "v9+p+B/WTIHCwYmij6J7e7/8PHsUC9BQmmJHQA12zLTqLoHYo+GBh0SFvgYiNNd/DDuw4IHYJxaK/ibAeyI4vhu88Vwf"
    "JTl20RDAppYA74zoDZumFcD3N64wf0AkeDQgIoR6TCTDPun5vqJvXlTpLXxe/zjZ3woqkMBJQwCLWgJsLIyH2rxYLw1v"
    "zoEbpxuYfUMY6b0BeVwkQqxd+VeYWzoSZo/FPxRlzYGxz/aj9tXwCnkS2Ew0oqEkpRUB2uwpSoLa3NiQbJ8zQVM+qBgB"
    "f8SPMPT+PFxQAmk5tlAR4FBJihMBNXgbp4G/YcFZYMHQGO9f82JdApG5bTXRCmwsWy0B9o1PglV5vZiRYNnwWHgu0RIN"
    "EthNNIMYpZYEZCpgS4IYKErtzrYEeYISWKgKcLAkuQoBtdhdlAh1+XHMSECozuqJU4LKfxImvpsCCXqHy2e9iXYcnJBs"
    "RjwIqMmW0fiIxdC6gIwGkwb0oC5CRUYMbB2TCMvzeitqh/z7EHnMNqkR2LBdbQGC2TE2gQm2jE6A14bHQWE/+VMDWVvM"
    "GhLrLXzgPVKWwGVSM/AXtGotAWtswQIuRhlsA5+JKAQpeFFaD5g3tBdsKEyI2CZFCSyqCnAAhxcEOKHZjQtauf9WqQSV"
    "mTFOkxaBF+s4UIwXzaGOUgkQmzYSFCe7ecFUkiBXsQTZGgiQZEY8CHDos0yZBB7ErLoE+4uTkhEPAhz6KJRAm6mAS8Cs"
    "BHaTVoEXatk/PsmNAIc+MiVwmLQMvFAz4uQFU0sCyQdKqkx6xKfjk6yIBwEOXabhc74EASwmvQIv1ow4eNGo4UK8BSWL"
    "O2Z7f2cREi2IE8ERIRE4kqlHOj3Tk+f89kc9dovfQYSiRDNi21eU6EaAE5FWxI75ijiEk+f89tHAThZ8pPC6DvtiA2/O"
    "jFgRB+Jqv+GuWmxPew6c+552EPYLqIEg2VGO2cSDBw8ePHjw4MGDBw8ePHjoE/8HObNCAJCkRQQAAAAASUVORK5CYII="
)


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("Python 3.9+ required.", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
