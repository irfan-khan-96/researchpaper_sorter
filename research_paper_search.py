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
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, NamedTuple, Optional

# Optional fast path: pdfplumber for text, rapidfuzz for fuzzy scoring
try:
    import pdfplumber

    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

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


DB_FILENAME = os.environ.get("RPS_DB_FILENAME", ".pdf_search_index.db")
MAX_FRONT_PAGES = _env_int("RPS_MAX_FRONT_PAGES", 4)
ABSTRACT_CHARS = _env_int("RPS_ABSTRACT_CHARS", 1200)
# Full body text goes into the FTS index so the whole paper is searchable.
# 0 means "no cap"; the default bounds pathological/huge PDFs.
FULLTEXT_CHARS = _env_int("RPS_FULLTEXT_CHARS", 200000)
# A short slice of the body kept in the base table for display/snippets only.
PREVIEW_CHARS = _env_int("RPS_PREVIEW_CHARS", 4000)
SEARCH_CANDIDATE_LIMIT = _env_int("RPS_SEARCH_CANDIDATE_LIMIT", 240)
INDEX_COMMIT_BATCH = _env_int("RPS_INDEX_COMMIT_BATCH", 20)
SQLITE_BUSY_MS = _env_int("RPS_SQLITE_BUSY_MS", 5000)
INDEX_WORKERS = _env_int("RPS_INDEX_WORKERS", 0)  # 0 = auto (detect CPU count)
# Result cards built per event-loop tick; keeps the search box responsive.
RENDER_BATCH_SIZE = _env_int("RPS_RENDER_BATCH", 4)
# Only surface genuine research papers / institutional reports; discard the rest.
REQUIRE_SCHOLARLY = os.environ.get("RPS_REQUIRE_SCHOLARLY", "1") not in ("0", "false", "False", "")
# How many scholarly signals a document needs to be treated as a paper/report.
SCHOLARLY_MIN_SIGNALS = _env_int("RPS_SCHOLARLY_MIN_SIGNALS", 2)

# Weights for the Python re-ranking pass. It runs only over the small,
# high-signal fields loaded per candidate; the full body is ranked by BM25 in
# SQLite, not here, so it must not appear in this map.
SCORE_WEIGHTS = {
    "title": 5.0,
    "keywords": 4.0,
    "abstract": 3.0,
    "authors": 2.0,
    "preview": 1.0,
}

# Columns pulled into Python per candidate. `fulltext` is deliberately excluded
# so large bodies never enter the hot search path (BM25 reads it in SQLite).
RESULT_COLUMNS = (
    "id", "path", "filename", "title", "authors", "year",
    "abstract", "keywords", "preview", "doi", "arxiv_id",
)
_FTS_SELECT = ", ".join(f"papers.{col}" for col in RESULT_COLUMNS)
_ROW_SELECT = ", ".join(RESULT_COLUMNS)

# Fields the SQL fallback (no FTS5) substring-scans — includes the full body so
# the degraded path can still find body matches.
SEARCH_FIELDS = ("title", "keywords", "abstract", "authors", "fulltext")

YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
ARXIV_RE = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


# -----------------------------------------------------------------------------
# PDF TEXT EXTRACTION
# -----------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace and trim leading or trailing junk."""
    return re.sub(r"\s+", " ", text).strip()


def _accumulate_pages(
    page_texts,
    max_front_pages: int,
    char_limit: int,
) -> tuple[list[str], str]:
    """
    Consume per-page text, returning the first ``max_front_pages`` pages verbatim
    (for field extraction) and the concatenated body up to ``char_limit`` chars
    (``char_limit <= 0`` means the whole document).
    """
    front_pages: list[str] = []
    text_parts: list[str] = []
    collected = 0
    unlimited = char_limit <= 0

    for page_index, text in enumerate(page_texts):
        want_more = unlimited or collected < char_limit
        if page_index >= max_front_pages and not want_more:
            break
        if page_index < max_front_pages:
            front_pages.append(text)
        if text and want_more:
            chunk = text if unlimited else text[: char_limit - collected]
            text_parts.append(chunk)
            collected += len(chunk)

    return front_pages, " ".join(text_parts)


def _extract_pdf_content(
    pdf_path: str,
    max_front_pages: int = MAX_FRONT_PAGES,
    char_limit: int = FULLTEXT_CHARS,
) -> tuple[list[str], str]:
    """
    Extract the front pages and the full body text in a single pass, so the whole
    paper is searchable. Front pages feed structured field extraction; the body
    (capped at ``char_limit``) is indexed for full-text search.
    """
    if PDFPLUMBER_OK:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                def texts():
                    for page in pdf.pages:
                        try:
                            yield page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                        except Exception:
                            yield ""

                return _accumulate_pages(texts(), max_front_pages, char_limit)
        except Exception:
            LOGGER.exception("pdfplumber extraction failed for %s", pdf_path)

    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)

        def texts():
            for page in reader.pages:
                try:
                    yield page.extract_text() or ""
                except Exception:
                    yield ""

        return _accumulate_pages(texts(), max_front_pages, char_limit)
    except Exception:
        LOGGER.exception("PDF extraction failed for %s", pdf_path)
        return [], ""


# -----------------------------------------------------------------------------
# FIELD EXTRACTION
# -----------------------------------------------------------------------------

# Names of reputable publishers, databases, and institution types. Presence of
# any of these (as one of several signals) marks a document as a genuine paper
# or institutional report rather than an arbitrary PDF.
REPUTABLE_MARKERS = (
    "doi.org", "arxiv", "biorxiv", "medrxiv", "ssrn", "pubmed", "pmc",
    "ieee", "acm", "elsevier", "springer", "nature", "wiley", "sciencedirect",
    "jstor", "taylor & francis", "sage", "oxford university press",
    "cambridge university press", "plos", "mdpi", "frontiers", "bmj", "aaas",
    "proceedings of", "journal of", "university", "institute", "laboratory",
    "national laboratory", "department of", "technical report", "working paper",
    "world health organization", "nasa", "nist", "cern", "oecd", "unesco",
    "european commission", "national institutes of health", "research council",
)
_REPUTABLE_RE = re.compile("|".join(re.escape(marker) for marker in REPUTABLE_MARKERS), re.IGNORECASE)
_REFERENCES_RE = re.compile(r"\b(references|bibliography|works cited)\b", re.IGNORECASE)
_INTRO_RE = re.compile(r"\b(introduction|abstract|methodology|methods)\b", re.IGNORECASE)
_CITATION_RE = re.compile(r"\[\d{1,3}\]")


def classify_document(fields: dict) -> tuple[bool, str]:
    """
    Decide whether an extracted document is a genuine research paper / report.

    Uses several independent scholarly signals (DOI, arXiv id, an abstract, a
    references section, a reputable publisher/institution name, in-text numeric
    citations). A document is accepted when it shows at least
    ``SCHOLARLY_MIN_SIGNALS`` of them — forgiving enough not to drop real papers,
    strict enough to discard invoices, slides, receipts, and the like. Returns
    ``(is_scholarly, reason)``.
    """
    text = " ".join((
        fields.get("title", ""),
        fields.get("abstract", ""),
        fields.get("keywords", ""),
        fields.get("fulltext", ""),
    ))
    signals: list[str] = []
    if fields.get("doi"):
        signals.append("DOI")
    if fields.get("arxiv_id"):
        signals.append("arXiv id")
    if fields.get("abstract"):
        signals.append("abstract")
    if _REFERENCES_RE.search(text):
        signals.append("references")
    if _INTRO_RE.search(text):
        signals.append("paper sections")
    if len(_CITATION_RE.findall(text)) >= 3:
        signals.append("numbered citations")
    marker = _REPUTABLE_RE.search(text)
    if marker:
        signals.append(f"source '{marker.group(0).lower()}'")

    is_scholarly = len(signals) >= max(1, SCHOLARLY_MIN_SIGNALS)
    reason = ", ".join(signals) if signals else "no scholarly markers found"
    return is_scholarly, reason


def _extract_fields(pdf_path: str) -> dict[str, str]:
    """
    Extract structured fields from a research paper PDF. Returns a dict with:
    title, authors, year, abstract, keywords, fulltext, preview, doi, arxiv_id.
    ``fulltext`` is the whole body (for the FTS index); ``preview`` is a short
    slice kept for display.
    """
    pages, body = _extract_pdf_content(
        pdf_path,
        max_front_pages=MAX_FRONT_PAGES,
        char_limit=FULLTEXT_CHARS,
    )
    if not pages:
        return {}

    clean_body = _clean(body)

    page0 = pages[0]
    combined_front = "\n".join(pages[:2])

    title = _guess_title(page0)
    authors = _guess_authors(page0, title)

    year_matches = YEAR_RE.findall(combined_front)
    year = year_matches[0] if year_matches else ""

    abstract = _extract_section(
        combined_front,
        "abstract",
        next_sections=["introduction", "keywords", "key words", "1.", "1 "],
    )

    keywords = _extract_section(
        combined_front,
        r"key\s*words?",
        next_sections=["abstract", "introduction", "1.", "1 "],
        max_chars=400,
    )

    doi_match = DOI_RE.search(combined_front)
    arxiv_match = ARXIV_RE.search(combined_front)

    fields: dict[str, object] = {
        "title": _clean(title)[:300],
        "authors": _clean(authors)[:300],
        "year": year,
        "abstract": _clean(abstract)[:ABSTRACT_CHARS],
        "keywords": _clean(keywords)[:400],
        "fulltext": clean_body,
        "preview": clean_body[:PREVIEW_CHARS],
        "doi": doi_match.group(0)[:200] if doi_match else "",
        "arxiv_id": arxiv_match.group(1)[:50] if arxiv_match else "",
    }
    is_scholarly, reason = classify_document(fields)
    fields["is_scholarly"] = 1 if is_scholarly else 0
    fields["doc_reason"] = reason[:200]
    return fields


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
    """Return likely author lines between the title and abstract."""
    lines = page0.splitlines()
    title_norm = title.lower().strip()
    in_zone = False
    author_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if title_norm and (stripped.lower() == title_norm or title_norm in stripped.lower()):
            in_zone = True
            continue
        if in_zone:
            if re.match(r"abstract\b", stripped, re.IGNORECASE):
                break
            if len(stripped) > 5 and not re.match(r"^\d+\.?\s", stripped):
                author_lines.append(stripped)
            if len(author_lines) >= 3:
                break
    return "; ".join(author_lines)


def _extract_section(
    text: str,
    header: str,
    next_sections: list[str],
    max_chars: int = ABSTRACT_CHARS,
) -> str:
    """Find the text under a section header and stop at the next section header."""
    pattern = re.compile(
        r"(?:^|\n)\s*" + header + r"\s*[:\.]?\s*\n?(.*?)(?=\n\s*(?:"
        + "|".join(re.escape(section) for section in next_sections)
        + r")\s*[:\.]?\s*\n|$)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if match:
        return match.group(1).strip()[:max_chars]
    return ""


# -----------------------------------------------------------------------------
# SQLITE INDEX
# -----------------------------------------------------------------------------

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id          INTEGER PRIMARY KEY,
    path        TEXT    UNIQUE NOT NULL,
    mtime       REAL    NOT NULL,
    size        INTEGER NOT NULL,
    filename    TEXT    NOT NULL,
    title       TEXT    DEFAULT '',
    authors     TEXT    DEFAULT '',
    year        TEXT    DEFAULT '',
    abstract    TEXT    DEFAULT '',
    keywords    TEXT    DEFAULT '',
    fulltext    TEXT    DEFAULT '',   -- whole body; indexed by FTS for search
    preview     TEXT    DEFAULT '',   -- short slice of the body; display only
    doi         TEXT    DEFAULT '',
    arxiv_id    TEXT    DEFAULT '',
    is_scholarly INTEGER DEFAULT 1,   -- 1 = genuine paper/report, 0 = discarded
    doc_reason  TEXT    DEFAULT '',   -- why it was (not) classified as scholarly
    indexed_at  TEXT    DEFAULT (datetime('now'))
);
-- path is already indexed by its UNIQUE constraint; only year needs its own
-- index (it is the sole structured filter used by search).
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
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
INDEX_FORMAT_VERSION = 3


def get_db_path(directory: str) -> str:
    return os.path.join(directory, DB_FILENAME)


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BASE_SCHEMA)

    # Add columns introduced after the original release (idempotent for old DBs).
    existing = _table_columns(conn, "papers")
    if "preview" not in existing:
        conn.execute("ALTER TABLE papers ADD COLUMN preview TEXT DEFAULT ''")
    if "is_scholarly" not in existing:
        conn.execute("ALTER TABLE papers ADD COLUMN is_scholarly INTEGER DEFAULT 1")
    if "doc_reason" not in existing:
        conn.execute("ALTER TABLE papers ADD COLUMN doc_reason TEXT DEFAULT ''")

    # One-time re-index when the extraction format changes (e.g. whole-body FTS).
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < INDEX_FORMAT_VERSION:
        if version:  # a brand-new database has nothing to re-index
            LOGGER.info(
                "Index format v%d -> v%d; clearing cache to re-index full text.",
                version, INDEX_FORMAT_VERSION,
            )
        conn.execute("DELETE FROM papers")
        conn.execute(f"PRAGMA user_version = {INDEX_FORMAT_VERSION}")

    if FTS5_AVAILABLE:
        conn.executescript(FTS_SCHEMA)
        papers_exist = conn.execute("SELECT EXISTS(SELECT 1 FROM papers LIMIT 1)").fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
        if papers_exist and fts_rows == 0:
            conn.execute("INSERT INTO papers_fts(papers_fts) VALUES ('rebuild')")
    else:
        LOGGER.warning("FTS5 unavailable in this SQLite build; using SQL fallback search.")
    conn.commit()


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_MS / 1000)
    _configure_connection(conn)
    _ensure_schema(conn)
    return conn


def needs_indexing(conn: sqlite3.Connection, path: str, mtime: float, size: int) -> bool:
    row = conn.execute("SELECT mtime, size FROM papers WHERE path = ?", (path,)).fetchone()
    if row is None:
        return True
    return abs(row["mtime"] - mtime) > 0.01 or row["size"] != size


def upsert_paper(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    size: int,
    fields: dict[str, str],
) -> None:
    conn.execute(
        """
        INSERT INTO papers (path, mtime, size, filename, title, authors, year,
                            abstract, keywords, fulltext, preview, doi, arxiv_id,
                            is_scholarly, doc_reason, indexed_at)
        VALUES (:path, :mtime, :size, :filename, :title, :authors, :year,
                :abstract, :keywords, :fulltext, :preview, :doi, :arxiv_id,
                :is_scholarly, :doc_reason, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            mtime = excluded.mtime,
            size = excluded.size,
            filename = excluded.filename,
            title = excluded.title,
            authors = excluded.authors,
            year = excluded.year,
            abstract = excluded.abstract,
            keywords = excluded.keywords,
            fulltext = excluded.fulltext,
            preview = excluded.preview,
            doi = excluded.doi,
            arxiv_id = excluded.arxiv_id,
            is_scholarly = excluded.is_scholarly,
            doc_reason = excluded.doc_reason,
            indexed_at = excluded.indexed_at
        """,
        {
            "path": path,
            "mtime": mtime,
            "size": size,
            "filename": os.path.basename(path),
            # Callers that seed rows directly (not via extraction) default to
            # scholarly so they are not silently hidden from search.
            "is_scholarly": int(fields.get("is_scholarly", 1)),
            "doc_reason": str(fields.get("doc_reason", "")),
            **{
                key: fields.get(key, "")
                for key in ("title", "authors", "year", "abstract", "keywords",
                            "fulltext", "preview", "doi", "arxiv_id")
            },
        },
    )


def remove_missing(conn: sqlite3.Connection, existing_paths: set[str]) -> None:
    indexed_paths = {row["path"] for row in conn.execute("SELECT path FROM papers")}
    missing_paths = indexed_paths - existing_paths
    if not missing_paths:
        return
    conn.executemany("DELETE FROM papers WHERE path = ?", ((path,) for path in missing_paths))


def count_nonscholarly(conn: sqlite3.Connection) -> int:
    """How many indexed documents were classified as non-papers (discarded)."""
    try:
        return conn.execute("SELECT COUNT(*) FROM papers WHERE is_scholarly = 0").fetchone()[0]
    except sqlite3.Error:
        return 0


def iter_pdf_files(directory: str) -> list[Path]:
    pdf_paths: list[Path] = []
    for root, _, files in os.walk(directory):
        for name in files:
            if name.lower().endswith(".pdf"):
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
    indexed = errors = done = pending = 0
    cancelled = False

    conn = open_db(db_path)
    try:
        pdf_files = iter_pdf_files(directory)
        total = len(pdf_files)
        existing_paths = {str(path) for path in pdf_files}

        if force_reindex:
            conn.execute("DELETE FROM papers")
        remove_missing(conn, existing_paths)
        conn.commit()

        # Decide which files actually need work before spending any CPU on them.
        to_index: list[tuple[str, float, int]] = []
        for pdf_path in pdf_files:
            try:
                stat = pdf_path.stat()
            except OSError:
                continue
            path_str = str(pdf_path)
            if force_reindex or needs_indexing(conn, path_str, stat.st_mtime, stat.st_size):
                to_index.append((path_str, stat.st_mtime, stat.st_size))
        skipped = total - len(to_index)

        def store(path_str: str, mtime: float, size: int, fields, exc) -> None:
            nonlocal indexed, errors, done, pending
            processed.add(path_str)
            done += 1
            report(done, len(to_index), os.path.basename(path_str))
            if exc is not None:
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{os.path.basename(path_str)}: {exc}")
                LOGGER.warning("Failed to index %s: %s", path_str, exc)
                return
            upsert_paper(conn, path_str, mtime, size, fields)
            indexed += 1
            pending += 1
            if pending >= INDEX_COMMIT_BATCH:
                conn.commit()
                pending = 0

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

        conn.commit()
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
    return [term.lower() for term in re.split(r"[\s,;]+", query.strip()) if len(term) >= 2]


def _fts_query(terms: list[str]) -> str:
    fts_terms: list[str] = []
    for term in terms:
        cleaned = re.sub(r"[^0-9A-Za-z]+", " ", term).strip()
        for token in cleaned.split():
            if len(token) >= 2:
                fts_terms.append(f'"{token}"*')
    return " OR ".join(dict.fromkeys(fts_terms))


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


def _fetch_candidate_rows(
    conn: sqlite3.Connection,
    terms: list[str],
    year_range: Optional[tuple[int, int]],
    top_n: int,
) -> list[sqlite3.Row]:
    limit = max(SEARCH_CANDIDATE_LIMIT, top_n * 10)

    if FTS5_AVAILABLE:
        fts_query = _fts_query(terms)
        if fts_query:
            try:
                sql = f"""
                    SELECT {_FTS_SELECT}
                    FROM papers_fts
                    JOIN papers ON papers.id = papers_fts.rowid
                    WHERE papers_fts MATCH ?
                """
                params: list[object] = [fts_query]
                if year_range:
                    sql += " AND CAST(papers.year AS INTEGER) BETWEEN ? AND ?"
                    params.extend(year_range)
                if REQUIRE_SCHOLARLY:
                    sql += " AND papers.is_scholarly = 1"
                # One weight per FTS column, in declared order:
                # title, authors, year, abstract, keywords, fulltext.
                sql += " ORDER BY bm25(papers_fts, 5.0, 2.0, 1.0, 3.0, 4.0, 1.0) LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                LOGGER.warning("FTS query failed; falling back to SQL scan.", exc_info=True)

    where_clauses: list[str] = []
    params: list[object] = []
    if year_range:
        where_clauses.append("CAST(year AS INTEGER) BETWEEN ? AND ?")
        params.extend(year_range)
    if REQUIRE_SCHOLARLY:
        where_clauses.append("is_scholarly = 1")

    term_clauses: list[str] = []
    for term in terms:
        field_checks = [f"instr(lower({field}), ?) > 0" for field in SEARCH_FIELDS]
        term_clauses.append("(" + " OR ".join(field_checks) + ")")
        params.extend([term] * len(SEARCH_FIELDS))

    if term_clauses:
        where_clauses.append("(" + " OR ".join(term_clauses) + ")")

    sql = f"SELECT {_ROW_SELECT} FROM papers"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def score_paper(terms: list[str], row: sqlite3.Row) -> float:
    """
    Bonus score from exact/fuzzy matches in the short, high-signal fields. This
    refines the BM25 candidate order; it does not gate results (a paper whose
    only match is deep in the body scores 0 here but is still a valid hit).
    """
    total = 0.0
    fields = {name: (row[name] or "") for name in SCORE_WEIGHTS}
    lower_fields = {name: value.lower() for name, value in fields.items()}

    for term in terms:
        for name, weight in SCORE_WEIGHTS.items():
            if term in lower_fields[name]:
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
    rows = _fetch_candidate_rows(conn, terms, year_range, top_n=top_n)
    # Candidates arrive best-first (BM25 for FTS). Keep that as a base score so a
    # paper matched only deep in the body is retained, then let the field-level
    # bonus lift exact title/keyword hits above it.
    total_rows = len(rows)
    results: list[tuple[float, dict[str, object]]] = []
    for position, row in enumerate(rows):
        base = total_rows - position
        results.append((base + score_paper(terms, row), dict(row)))

    results.sort(key=lambda item: -item[0])
    max_score = results[0][0] if results else 1.0

    output: list[dict[str, object]] = []
    for raw_score, result in results[:top_n]:
        result["relevance"] = min(100, int(raw_score / max(max_score, 1) * 100))
        output.append(result)
    return output


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
        tokens = keyword.split()
        if not tokens:
            continue
        body = r"\s+".join(re.escape(token) for token in tokens)
        patterns.append(re.compile(rf"(?<!\w){body}(?!\w)", flags))
    return patterns


def paper_matches(row, patterns: list[re.Pattern]) -> bool:
    """A paper matches only if EVERY keyword pattern is found in its text."""
    text = " ".join(str(row[field] or "") for field in MATCH_FIELDS)
    return all(pattern.search(text) for pattern in patterns)


def _candidate_rows(conn: sqlite3.Connection, keywords: list[str]) -> list[sqlite3.Row]:
    """
    Narrow to papers that plausibly contain all keywords. FTS does this fast over
    huge collections; the caller still verifies each hit exactly. Falls back to a
    full scan when FTS5 is unavailable (correctness is unaffected either way).
    """
    fields = ", ".join(f"papers.{field}" for field in MATCH_FIELDS)
    columns = f"{fields}, papers.path, papers.is_scholarly"
    if FTS5_AVAILABLE:
        phrases = []
        for keyword in keywords:
            tokens = re.sub(r'"', " ", keyword).split()
            if tokens:
                phrases.append('"' + " ".join(tokens) + '"')
        if phrases:
            match_query = " AND ".join(phrases)
            try:
                sql = (
                    f"SELECT {columns} FROM papers_fts "
                    "JOIN papers ON papers.id = papers_fts.rowid "
                    "WHERE papers_fts MATCH ?"
                )
                return conn.execute(sql, [match_query]).fetchall()
            except sqlite3.OperationalError:
                LOGGER.warning("Aggregation FTS query failed; scanning all rows.", exc_info=True)

    plain = ", ".join(MATCH_FIELDS)
    return conn.execute(f"SELECT {plain}, path, is_scholarly FROM papers").fetchall()


def _hash_file(path: str, chunk: int = 1 << 20) -> str:
    """Content hash used to recognise a paper already present in the destination."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_into(paths: list[str], dest_dir: str) -> tuple[int, int, int]:
    """
    Copy files into ``dest_dir``, skipping any paper whose content is already
    there (identical bytes), and de-duplicating merely-colliding names. Existing
    files are hashed lazily and only when a source shares their size, so the
    identity check stays cheap. Returns ``(copied, errors, skipped_existing)``.
    """
    os.makedirs(dest_dir, exist_ok=True)

    dest_by_size: dict[int, list[str]] = {}
    existing_names: set[str] = set()
    for entry in os.scandir(dest_dir):
        if entry.is_file():
            existing_names.add(entry.name)
            with suppress(OSError):
                dest_by_size.setdefault(entry.stat().st_size, []).append(entry.path)

    present_hashes: dict[int, set[str]] = {}
    hashed_sizes: set[int] = set()

    def hashes_for(size: int) -> set[str]:
        bucket = present_hashes.setdefault(size, set())
        if size not in hashed_sizes:  # hash same-size dest files once, on demand
            for existing in dest_by_size.get(size, []):
                with suppress(OSError):
                    bucket.add(_hash_file(existing))
            hashed_sizes.add(size)
        return bucket

    copied = errors = skipped_existing = 0
    for path in paths:
        try:
            size = os.path.getsize(path)
            digest = _hash_file(path)
        except OSError as exc:
            errors += 1
            LOGGER.warning("Could not read %s: %s", path, exc)
            continue

        if digest in hashes_for(size):
            skipped_existing += 1
            continue

        base = os.path.basename(path)
        target = os.path.join(dest_dir, base)
        if base in existing_names or os.path.exists(target):
            stem, ext = os.path.splitext(base)
            counter = 1
            while True:
                candidate_name = f"{stem} ({counter}){ext}"
                candidate = os.path.join(dest_dir, candidate_name)
                if candidate_name not in existing_names and not os.path.exists(candidate):
                    base, target = candidate_name, candidate
                    break
                counter += 1
        try:
            shutil.copy2(path, target)
        except OSError as exc:
            errors += 1
            LOGGER.warning("Could not copy %s: %s", path, exc)
            continue
        copied += 1
        existing_names.add(base)
        present_hashes.setdefault(size, set()).add(digest)  # catch within-run dupes
    return copied, errors, skipped_existing


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
    "index" and "match" phases; ``should_cancel()`` allows interruption.
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
    matched: list[str] = []
    skipped_nonpaper = 0
    cancelled = False
    try:
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
            if paper_matches(row, patterns):
                # Only genuine papers/reports are collected; the rest are counted
                # so the user can be told they were excluded.
                if REQUIRE_SCHOLARLY and not row["is_scholarly"]:
                    skipped_nonpaper += 1
                else:
                    matched.append(path)
    finally:
        with suppress(sqlite3.Error):
            conn.close()

    if cancelled:
        return AggregateStats(index_stats.total, candidates, len(matched), 0,
                              0, skipped_nonpaper, index_stats.errors, True, dest_dir)

    copied, copy_errors, skipped_existing = _copy_into(matched, dest_dir)
    return AggregateStats(
        index_stats.total, candidates, len(matched), copied,
        skipped_existing, skipped_nonpaper, index_stats.errors + copy_errors,
        False, dest_dir,
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

    def __init__(self, parent, result: dict, on_open, on_reveal, **kwargs):
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
        self._build(result)
        self.bind("<Enter>", self._hover_on, add="+")
        self.bind("<Leave>", self._hover_off, add="+")
        self.bind("<Double-Button-1>", lambda _event: on_open(result["path"]), add="+")

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
        if stats.errors:
            lines.append(f"{stats.errors} error(s) — see logs.")

        color = (
            THEME["green"] if stats.copied
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
        collect_btn.pack(side="left", padx=(0, 14))
        Tooltip(collect_btn, "Copy papers that exactly contain ALL keywords to a folder")

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
        if not PDFPLUMBER_OK:
            missing.append("pdfplumber")
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
            discarded = 0
            if REQUIRE_SCHOLARLY and self._search_conn:
                discarded = count_nonscholarly(self._search_conn)
            if discarded:
                message += f" — {discarded} non-paper doc(s) hidden"
            has_note = bool(stats.errors or discarded)
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

        if not query:
            self._last_query = ""
            self._clear_results()
            return

        self._last_query = query

        if not self._search_conn:
            self._set_status("Please select a directory first.", color=THEME["highlight"])
            return

        try:
            results = search(self._search_conn, query, year_filter=year)
            self._render_results(results)
        except sqlite3.Error as exc:
            self._set_status("Search failed. Please try reloading the directory.", color=THEME["red"])
            LOGGER.exception("Search failed: %s", exc)

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
    if REQUIRE_SCHOLARLY:
        with suppress(sqlite3.Error):
            conn = open_db(get_db_path(directory))
            try:
                discarded = count_nonscholarly(conn)
            finally:
                conn.close()
            if discarded:
                print(f"  {discarded} document(s) excluded as non-papers.")
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
        label = "Indexing" if phase == "index" else "Scanning"
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
    "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"
)


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("Python 3.9+ required.", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
