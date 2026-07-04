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
import logging
import os
import re
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
FULLTEXT_CHARS = _env_int("RPS_FULLTEXT_CHARS", 4000)
SNIPPET_LEN = _env_int("RPS_SNIPPET_LEN", 280)
SEARCH_CANDIDATE_LIMIT = _env_int("RPS_SEARCH_CANDIDATE_LIMIT", 240)
INDEX_COMMIT_BATCH = _env_int("RPS_INDEX_COMMIT_BATCH", 20)
SQLITE_BUSY_MS = _env_int("RPS_SQLITE_BUSY_MS", 5000)
INDEX_WORKERS = _env_int("RPS_INDEX_WORKERS", 0)  # 0 = auto (detect CPU count)

FIELD_WEIGHTS = {
    "title": 5.0,
    "keywords": 4.0,
    "abstract": 3.0,
    "authors": 2.0,
    "fulltext": 1.0,
}

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


def _extract_pdf_content(
    pdf_path: str,
    max_front_pages: int = MAX_FRONT_PAGES,
    char_limit: int = FULLTEXT_CHARS,
) -> tuple[list[str], str]:
    """
    Extract both the front pages and limited full text in a single pass.
    This avoids opening and parsing the same PDF twice.
    """
    front_pages: list[str] = []
    text_parts: list[str] = []
    collected = 0

    if PDFPLUMBER_OK:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page_index, page in enumerate(pdf.pages):
                    if page_index >= max_front_pages and collected >= char_limit:
                        break
                    try:
                        text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                    except Exception:
                        text = ""

                    if page_index < max_front_pages:
                        front_pages.append(text)

                    if text and collected < char_limit:
                        remaining = char_limit - collected
                        text_parts.append(text[:remaining])
                        collected += len(text)
            return front_pages, " ".join(text_parts)
        except Exception:
            LOGGER.exception("pdfplumber extraction failed for %s", pdf_path)

    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        for page_index, page in enumerate(reader.pages):
            if page_index >= max_front_pages and collected >= char_limit:
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""

            if page_index < max_front_pages:
                front_pages.append(text)

            if text and collected < char_limit:
                remaining = char_limit - collected
                text_parts.append(text[:remaining])
                collected += len(text)
        return front_pages, " ".join(text_parts)
    except Exception:
        LOGGER.exception("PDF extraction failed for %s", pdf_path)
        return [], ""


# -----------------------------------------------------------------------------
# FIELD EXTRACTION
# -----------------------------------------------------------------------------

def _extract_fields(pdf_path: str) -> dict[str, str]:
    """
    Extract structured fields from a research paper PDF.
    Returns dict with: title, authors, year, abstract, keywords, fulltext, doi, arxiv_id
    """
    pages, fulltext = _extract_pdf_content(
        pdf_path,
        max_front_pages=MAX_FRONT_PAGES,
        char_limit=FULLTEXT_CHARS,
    )
    if not pages:
        return {}

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

    return {
        "title": _clean(title)[:300],
        "authors": _clean(authors)[:300],
        "year": year,
        "abstract": _clean(abstract)[:ABSTRACT_CHARS],
        "keywords": _clean(keywords)[:400],
        "fulltext": _clean(fulltext)[:FULLTEXT_CHARS],
        "doi": doi_match.group(0)[:200] if doi_match else "",
        "arxiv_id": arxiv_match.group(1)[:50] if arxiv_match else "",
    }


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
    fulltext    TEXT    DEFAULT '',
    doi         TEXT    DEFAULT '',
    arxiv_id    TEXT    DEFAULT '',
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


def get_db_path(directory: str) -> str:
    return os.path.join(directory, DB_FILENAME)


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BASE_SCHEMA)
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
                            abstract, keywords, fulltext, doi, arxiv_id, indexed_at)
        VALUES (:path, :mtime, :size, :filename, :title, :authors, :year,
                :abstract, :keywords, :fulltext, :doi, :arxiv_id, datetime('now'))
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
            doi = excluded.doi,
            arxiv_id = excluded.arxiv_id,
            indexed_at = excluded.indexed_at
        """,
        {
            "path": path,
            "mtime": mtime,
            "size": size,
            "filename": os.path.basename(path),
            **{
                key: fields.get(key, "")
                for key in ("title", "authors", "year", "abstract", "keywords", "fulltext", "doi", "arxiv_id")
            },
        },
    )


def remove_missing(conn: sqlite3.Connection, existing_paths: set[str]) -> None:
    indexed_paths = {row["path"] for row in conn.execute("SELECT path FROM papers")}
    missing_paths = indexed_paths - existing_paths
    if not missing_paths:
        return
    conn.executemany("DELETE FROM papers WHERE path = ?", ((path,) for path in missing_paths))


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
                        path_str, mtime, size = futures[future]
                        try:
                            store(path_str, mtime, size, future.result(), None)
                        except Exception as exc:  # extraction raised in a worker
                            store(path_str, mtime, size, None, exc)
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


def _fetch_candidate_rows(
    conn: sqlite3.Connection,
    terms: list[str],
    year_filter: Optional[str],
    top_n: int,
) -> list[sqlite3.Row]:
    limit = max(SEARCH_CANDIDATE_LIMIT, top_n * 10)

    if FTS5_AVAILABLE:
        fts_query = _fts_query(terms)
        if fts_query:
            try:
                sql = """
                    SELECT papers.*
                    FROM papers_fts
                    JOIN papers ON papers.id = papers_fts.rowid
                    WHERE papers_fts MATCH ?
                """
                params: list[object] = [fts_query]
                if year_filter:
                    sql += " AND papers.year = ?"
                    params.append(year_filter)
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
    if year_filter:
        where_clauses.append("year = ?")
        params.append(year_filter)

    term_clauses: list[str] = []
    for term in terms:
        field_checks = [f"instr(lower({field}), ?) > 0" for field in SEARCH_FIELDS]
        term_clauses.append("(" + " OR ".join(field_checks) + ")")
        params.extend([term] * len(SEARCH_FIELDS))

    if term_clauses:
        where_clauses.append("(" + " OR ".join(term_clauses) + ")")

    sql = "SELECT * FROM papers"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def score_paper(terms: list[str], row: sqlite3.Row) -> float:
    """Score a paper against a list of query terms."""
    total = 0.0
    fields = {field: (row[field] or "") for field in SEARCH_FIELDS}
    lower_fields = {field: value.lower() for field, value in fields.items()}

    for term in terms:
        for field, text in fields.items():
            weight = FIELD_WEIGHTS[field]
            if term in lower_fields[field]:
                total += weight * 100
                continue
            if field in ("title", "abstract", "keywords"):
                fuzzy_score = _fuzzy_score(term, text[:500])
                if fuzzy_score > 60:
                    total += weight * fuzzy_score * 0.5
    return total


def search(
    conn: sqlite3.Connection,
    query: str,
    year_filter: Optional[str] = None,
    top_n: int = 30,
) -> list[dict[str, object]]:
    """Search indexed papers and return sorted result dictionaries."""
    terms = _split_terms(query)
    if not terms:
        return []

    rows = _fetch_candidate_rows(conn, terms, year_filter, top_n=top_n)
    results: list[tuple[float, dict[str, object]]] = []
    for row in rows:
        score = score_paper(terms, row)
        if score > 0:
            results.append((score, dict(row)))

    results.sort(key=lambda item: -item[0])
    max_score = results[0][0] if results else 1.0

    output: list[dict[str, object]] = []
    for raw_score, result in results[:top_n]:
        result["relevance"] = min(100, int(raw_score / max(max_score, 1) * 100))
        output.append(result)
    return output


def make_snippet(text: str, terms: list[str], length: int = SNIPPET_LEN) -> str:
    """Return the most relevant window of text for display."""
    if not text:
        return ""
    lower = text.lower()
    best_pos = 0
    for term in terms:
        index = lower.find(term.lower())
        if index != -1:
            best_pos = max(0, index - 60)
            break
    snippet = text[best_pos : best_pos + length]
    if best_pos > 0:
        snippet = "..." + snippet
    if best_pos + length < len(text):
        snippet += "..."
    return snippet


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

    def __init__(self, parent, result: dict, terms: list[str], on_open, on_reveal, **kwargs):
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
        self._build(result, terms)
        self.bind("<Enter>", self._hover_on, add="+")
        self.bind("<Leave>", self._hover_off, add="+")
        self.bind("<Double-Button-1>", lambda _event: on_open(result["path"]), add="+")

    def _build(self, result: dict, terms: list[str]) -> None:
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
            for chip in re.split(r"[;,·]", keywords)[:8]:
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

        abstract = result.get("abstract") or result.get("fulltext", "")
        snippet = make_snippet(abstract, terms)
        if snippet:
            tk.Label(
                self,
                text=snippet,
                bg=THEME["card"],
                fg=THEME["subtext"],
                font=THEME["font_small"],
                wraplength=720,
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 6))

        footer = tk.Frame(self, bg=THEME["card"])
        footer.pack(fill="x", padx=12, pady=(0, 10))

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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Research Paper Search")
        self.geometry("980x720")
        self.minsize(760, 520)
        self.configure(bg=THEME["bg"])

        self._directory = ""
        self._db_path = ""
        self._search_conn: Optional[sqlite3.Connection] = None
        self._indexing = False
        self._index_job_id = 0
        self._active_job_id = 0
        self._pending_index_request: Optional[tuple[str, str, bool, int]] = None
        self._last_query = ""
        self._search_after_id = None
        self._closing = False

        self._setup_styles()
        self._build_ui()
        self._check_deps()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        ).pack(side="left", padx=(0, 14))

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
            text="Year",
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
            width=7,
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )
        year_entry.pack(side="left", ipady=6, padx=(0, 8))
        year_entry.bind("<Return>", lambda _event: self._do_search(), add="+")
        Tooltip(year_entry, "Filter by year, for example 2024")

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
            self._set_status(message, color=THEME["highlight"] if stats.errors else THEME["subtext"])
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
            terms = _split_terms(query)
            results = search(self._search_conn, query, year_filter=year)
            self._render_results(results, terms)
        except sqlite3.Error as exc:
            self._set_status("Search failed. Please try reloading the directory.", color=THEME["red"])
            LOGGER.exception("Search failed: %s", exc)

    # Results -----------------------------------------------------------------

    def _clear_results(self) -> None:
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

    def _render_results(self, results: list[dict], terms: list[str]) -> None:
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

        for result in results:
            card = ResultCard(
                self._cards_frame,
                result,
                terms,
                on_open=self._open_pdf,
                on_reveal=self._reveal_in_explorer,
            )
            card.pack(fill="x", padx=8, pady=4)

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
    for sample in stats.error_samples:
        print(f"  ! {sample}", file=sys.stderr)
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

    if args.index:
        return _run_headless_index(args.index, args.force, args.workers)

    App().mainloop()
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("Python 3.9+ required.", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())
