"""Indexing: incremental runs, migrations, process pool, and lock behaviour."""
import os
import sqlite3
import threading
import time

import pytest

import research_paper_search as rps
from pdfgen import make_pdf, paper_page


@pytest.mark.parametrize("workers", [1, 2])
def test_incremental_indexing(tmp_path, workers):
    make_pdf(tmp_path / "a.pdf", [paper_page()])
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 not a pdf")
    db = rps.get_db_path(str(tmp_path))
    first = rps.index_directory(str(tmp_path), db, workers=workers)
    assert (first.total, first.indexed, first.errors) == (2, 2, 0)
    assert rps.index_directory(str(tmp_path), db, workers=workers).skipped == 2
    os.remove(tmp_path / "a.pdf")
    rps.index_directory(str(tmp_path), db, workers=workers)
    conn = rps.open_db(db)
    assert [row[0] for row in conn.execute("SELECT filename FROM papers")] == ["broken.pdf"]
    conn.close()
    assert not list(tmp_path.glob("*.db*")), "index must not be written into the scanned folder"


def test_legacy_database_is_migrated_and_reindexed(tmp_path):
    db = rps.get_db_path(str(tmp_path))
    os.makedirs(os.path.dirname(db), exist_ok=True)
    legacy = sqlite3.connect(db)
    legacy.executescript(
        "CREATE TABLE papers(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, mtime REAL NOT NULL,"
        " size INTEGER NOT NULL, filename TEXT NOT NULL, title TEXT, authors TEXT, year TEXT,"
        " abstract TEXT, keywords TEXT, fulltext TEXT, doi TEXT, arxiv_id TEXT, indexed_at TEXT);"
        "CREATE VIRTUAL TABLE papers_fts USING fts5(title, authors, year, abstract, keywords, fulltext,"
        " content='papers', content_rowid='id');"
        "PRAGMA user_version = 3;"
    )
    legacy.execute("INSERT INTO papers(path, mtime, size, filename) VALUES ('x', 1, 1, 'x')")
    legacy.commit()
    legacy.close()

    conn = rps.open_db(db)
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(papers)")]
    assert {"is_scholarly", "visible", "has_text", "has_tail", "truncated"} <= set(columns)
    assert columns[-2:] == ["fulltext", "tail"], "large text must be stored last"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rps.INDEX_FORMAT_VERSION
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
    rps.upsert_paper(conn, "y.pdf", 1, 1, {"title": "Fresh paper"})
    conn.commit()
    assert rps.search(conn, "fresh")[0]["title"] == "Fresh paper"  # recreated FTS + triggers work
    conn.close()


def test_write_lock_not_held_while_extracting(tmp_path, monkeypatch):
    for i in range(4):
        make_pdf(tmp_path / f"p{i}.pdf", [paper_page()])
    db = rps.get_db_path(str(tmp_path))
    rps.open_db(db).close()

    def slow_extract(path):
        time.sleep(0.4)
        return {"title": os.path.basename(path), "fulltext": "x" * 300}

    monkeypatch.setattr(rps, "_extract_fields", slow_extract)
    indexer = threading.Thread(target=rps.index_directory, args=(str(tmp_path), db), kwargs={"workers": 1})
    indexer.start()
    time.sleep(0.6)  # the indexer has stored at least one row by now
    other = sqlite3.connect(db, timeout=0.2)
    try:
        other.execute("BEGIN IMMEDIATE")  # would raise "database is locked" if the lock were held
        other.execute("ROLLBACK")
    finally:
        other.close()
        indexer.join()


def test_macos_and_system_junk_is_skipped(tmp_path):
    make_pdf(tmp_path / "paper.pdf", [paper_page()])
    (tmp_path / "._paper.pdf").write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 4092)  # AppleDouble
    for folder in (".Trashes", ".TemporaryItems", "$RECYCLE.BIN", "FOUND.000", "System Volume Information"):
        (tmp_path / folder).mkdir()
        make_pdf(tmp_path / folder / "deleted.pdf", [paper_page()])
    (tmp_path / "sub").mkdir()
    make_pdf(tmp_path / "sub" / "kept.pdf", [paper_page()])
    names = sorted(path.name for path in rps.iter_pdf_files(str(tmp_path)))
    assert names == ["kept.pdf", "paper.pdf"]


def test_overrides_survive_reindexing(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    make_pdf(folder / "slides.pdf", [[(10, f"Slide {i}: notes about zeolitex catalysts.") for i in range(30)]])
    db = rps.get_db_path(str(folder))
    rps.index_directory(str(folder), db, workers=1)
    conn = rps.open_db(db)
    path = str(folder / "slides.pdf")
    assert conn.execute("SELECT visible FROM papers").fetchone()[0] == 0
    assert rps.search(conn, "zeolitex") == []

    rps.set_visibility(conn, path, True)
    assert [r["filename"] for r in rps.search(conn, "zeolitex")] == ["slides.pdf"]
    rps.index_directory(str(folder), db, workers=1, force_reindex=True)   # rows rebuilt
    assert [r["filename"] for r in rps.search(conn, "zeolitex")] == ["slides.pdf"]

    rps.set_visibility(conn, path, False)   # back to the classifier's verdict
    assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 0
    assert rps.search(conn, "zeolitex") == []
    conn.close()
