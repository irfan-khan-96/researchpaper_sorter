# Research Paper Search

A fast desktop tool for searching a folder of academic PDFs **by their content** —
title, authors, year, abstract, keywords, and body text — not just filenames.

Point it at a directory, and it builds a local SQLite index (using SQLite's
FTS5 full‑text engine when available) and gives you an instant, ranked,
search‑as‑you‑type interface. Only new or changed files are re‑read on each run,
so re‑opening a large library is near‑instant.

![status: single-file app](https://img.shields.io/badge/app-single--file-informational)

---

## Features

- **Content search** across title / authors / year / abstract / keywords / full text
  with per‑field relevance weighting (BM25 when FTS5 is present).
- **Year filtering by range** — narrow results to a single year (`2024`) or a
  range (`2020-2025`, `2020-` for onward, `-2019` up to a year).
- **Whole‑document full‑text search** — the entire body of each paper is indexed,
  so you can find a concept that appears anywhere (methods, results, discussion),
  not just in the title or abstract.
- **Exact‑match collection** — copy every paper that strictly contains *all* of
  your keywords out of a massive unsorted folder into a separate one, with zero
  false positives (whole‑word matching only). Papers already in the destination
  are detected by content and skipped (never duplicated), and you're told how
  many were skipped.
- **Scholarly‑only** — only genuine research papers and institutional reports are
  surfaced; other PDFs (invoices, slides, receipts…) are discarded, and the count
  is reported so nothing goes missing silently.
- **Incremental indexing** — files are skipped unless their size or modification
  time changed.
- **Parallel extraction** — PDF text extraction is fanned out across CPU cores,
  so first‑time indexing of a large library is dramatically faster.
- **Graceful degradation** — works with the standard library alone; optional
  `pdfplumber` / `rapidfuzz` make it faster and more accurate.
- **Two entry points** — a Tkinter desktop UI, and a headless `--index` command
  for building the cache from scripts or a scheduled job.

---

## Requirements

- **Python 3.9+** (developed/tested on 3.14).
- Dependencies in [`requirements.txt`](requirements.txt). All are optional
  accelerators except that *at least one* PDF backend (`pdfplumber` or `pypdf`)
  is needed to read PDFs; installing everything is recommended.
- Tkinter for the GUI (bundled with CPython on Windows/macOS; on Debian/Ubuntu:
  `sudo apt install python3-tk`).

```bash
pip install -r requirements.txt
```

---

## Usage

### Desktop UI
```bash
python research_paper_search.py
```
Choose a folder, wait for the one‑time index to build, then type to search. Use
the **Years** box to filter by a single year or a range (`2024`, `2020-2025`,
`2020-`, `-2019`). Result cards stay compact — click **Show abstract** to expand
the full abstract for any result. Double‑click a result (or **Open PDF**) to open
it; **Reveal** shows it in your file manager.

### Headless indexing (admin/one‑off process)
Pre‑build or refresh the cache without opening a window — handy for large
libraries or scheduled jobs:
```bash
python research_paper_search.py --index /path/to/papers
python research_paper_search.py --index /path/to/papers --force        # ignore cache
python research_paper_search.py --index /path/to/papers --workers 4    # pin worker count
```
The next time you open that folder in the UI, search is immediate.

### Collect exact matches into a folder
Copy every paper that **exactly** contains *all* of the given keywords out of a
large unsorted folder into a separate one — like sorting a pile of downloads by
content. Matching is strict whole‑word (no substring, stem, or fuzzy hits), so
there are **zero false positives**; a paper qualifies only if every keyword is
present.

In the desktop UI, click **“Collect exact matches…”**, or headlessly:
```bash
python research_paper_search.py --index /unsorted/papers \
    --collect --dest /sorted/p53_cancer --keywords "p53, cancer"
python research_paper_search.py --index /unsorted/papers \
    --collect --dest /out --keywords "CRISPR" --match-case
```
Keywords are comma‑separated and may be phrases (e.g. `"machine learning"`).
Indexing is cached and parallel, so even a massive folder is fast after the first
pass; only the matching PDFs are copied (originals are left untouched). Papers
already present in the destination are recognised by content and **skipped**, and
documents that match but aren't genuine papers/reports are **discarded** — both
counts are reported so you always know what happened.

---

## Configuration

All operational settings are read from the **environment** with sensible
defaults — nothing is hard‑coded. Override any of these before launching:

| Variable | Default | Purpose |
|---|---|---|
| `RPS_DB_FILENAME` | `.pdf_search_index.db` | Index filename (stored inside the scanned folder) |
| `RPS_MAX_FRONT_PAGES` | `4` | Front pages scanned for title/authors/abstract |
| `RPS_ABSTRACT_CHARS` | `1200` | Max abstract characters stored |
| `RPS_FULLTEXT_CHARS` | `200000` | Max body characters indexed for full‑text search (`0` = no cap) |
| `RPS_PREVIEW_CHARS` | `4000` | Body slice kept for the light re‑rank pass |
| `RPS_SEARCH_CANDIDATE_LIMIT` | `240` | Candidate rows re‑ranked per query |
| `RPS_INDEX_COMMIT_BATCH` | `20` | Rows per DB commit while indexing |
| `RPS_SQLITE_BUSY_MS` | `5000` | SQLite busy timeout (ms) |
| `RPS_INDEX_WORKERS` | `0` | Extraction worker processes (`0` = auto‑detect CPUs, capped at 8) |
| `RPS_RENDER_BATCH` | `4` | Result cards drawn per UI tick (keeps the search box responsive) |
| `RPS_REQUIRE_SCHOLARLY` | `1` | Surface only papers/reports; set `0` to index every PDF |
| `RPS_SCHOLARLY_MIN_SIGNALS` | `2` | Scholarly signals a doc needs to be kept (lower = more permissive) |
| `RPS_LOG_LEVEL` | `WARNING` | Log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |

Example (PowerShell):
```powershell
$env:RPS_INDEX_WORKERS = "6"; $env:RPS_LOG_LEVEL = "INFO"
python research_paper_search.py --index C:\papers
```

Logs are written to **stderr** as a plain event stream; redirect or collect them
as you would any other process output.

---

## How it works

1. **Walk** the directory for `*.pdf` files.
2. **Extract** structured fields from the first few pages plus the **full body
   text** (in parallel worker processes).
3. **Classify** each document from scholarly signals (DOI, arXiv id, abstract,
   references, reputable publisher/institution names, numbered citations). Genuine
   papers/reports are kept; the rest are flagged and hidden from results.
4. **Store** them in SQLite; an FTS5 virtual table + triggers keep a whole‑document
   full‑text index in sync. Only a short `preview` of the body is kept in the base
   table for the re‑rank pass, so search stays fast.
5. **Search** runs an FTS/BM25 candidate query over the whole document, then
   re‑ranks candidates in Python with exact + fuzzy matching on the short,
   high‑signal fields for precise ordering.

**Exact collection** is separate and stricter: FTS only narrows candidates, then a
whole‑word/phrase regex decides matches (zero false positives), a paper must match
*every* keyword, non‑papers are excluded, and content‑identical files already in
the destination are skipped.

The index lives *inside the scanned folder* as `.pdf_search_index.db` (plus WAL
sidecars), so it travels with the papers and is ignored by git. Upgrading to a
new index format triggers a **one‑time re‑index** of that folder on next open.

---

## Twelve‑Factor notes

This is a desktop app, but it follows the applicable
[12‑factor](https://12factor.net/) principles:

- **II Dependencies** — declared explicitly in `requirements.txt`.
- **III Config** — every tunable is an `RPS_*` environment variable; no config
  baked into source.
- **IV Backing services** — the SQLite database is an attached resource located
  by configuration.
- **VI Processes** — indexing is a stateless function; all state lives in the
  attached database.
- **IX Disposability** — fast startup, graceful shutdown, and a process pool that
  is torn down cleanly on cancel/exit.
- **XI Logs** — emitted to stderr as an event stream at a configurable level.
- **XII Admin processes** — `--index` is a one‑off management command that reuses
  the same code path as the UI.

Port binding (VII) and horizontal process scaling (VIII) don't apply to a local
GUI tool.
