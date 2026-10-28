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
- **Scholarly‑only** — only genuine research papers and institutional reports
  (central banks, BIS, IMF, standards bodies, …) are surfaced; other PDFs
  (invoices, slides, receipts…) are hidden. **Hidden…** lists every hidden PDF
  with the reason, and you can include any of them; your choice survives
  re‑indexing.
- **Incremental indexing** — files are skipped unless their size or modification
  time changed.
- **Parallel extraction** — PDF text extraction is fanned out across CPU cores,
  so first‑time indexing of a large library is dramatically faster.
- **Fast PDF reading** — text is extracted with pdfium (the engine behind Chrome's
  PDF viewer), roughly 40× faster than pure‑Python readers; pypdf is the fallback.
- **Two entry points** — a Tkinter desktop UI, and a headless `--index` command
  for building the cache from scripts or a scheduled job.

---

## Requirements

- **Python 3.9+** (developed/tested on 3.14).
- Dependencies in [`requirements.txt`](requirements.txt): `pypdfium2` (primary
  PDF reader), `pypdf` (fallback), `rapidfuzz` (fuzzy scoring). At least one PDF
  reader is required; installing everything is recommended.
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
it; **Reveal** shows it in your file manager. **Hidden…** shows PDFs left out of
results (not judged a paper/report, or no extractable text) and lets you include
them; right‑click a result → **Hide from results** for the opposite case. macOS `._*` files and recycle‑bin/system folders are never indexed.

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
| `RPS_INDEX_DIR` | `%LOCALAPPDATA%\research_paper_search\indexes` (Windows), `~/Library/Caches/...` (macOS), `~/.cache/...` (Linux) | Where index files are kept (one per scanned folder) |
esearch_paper_search\indexes` (Windows), `~/Library/Caches/...` (macOS), `~/.cache/...` (Linux) | Where index files are kept (one per scanned folder) |
| `RPS_MAX_FRONT_PAGES` | `4` | Front pages scanned for title/authors/abstract |
| `RPS_ABSTRACT_CHARS` | `1200` | Max abstract characters stored |
| `RPS_FULLTEXT_CHARS` | `200000` | Body characters indexed for search (`0` = all); the rest is stored compressed for Collect |
| `RPS_MAX_TEXT_CHARS` | `5000000` | Safety cap on text read from one PDF (`0` = none) |
| `RPS_SEARCH_CANDIDATE_LIMIT` | `240` | Candidate rows re‑ranked per query |
| `RPS_INDEX_COMMIT_BATCH` | `20` | Rows per DB commit while indexing |
| `RPS_SQLITE_BUSY_MS` | `5000` | SQLite busy timeout (ms) |
| `RPS_INDEX_WORKERS` | `0` | Extraction worker processes (`0` = auto‑detect CPUs, capped at 8) |
| `RPS_RENDER_BATCH` | `4` | Result cards drawn per UI tick (keeps the search box responsive) |
| `RPS_REQUIRE_SCHOLARLY` | `1` | Surface only papers/reports; set `0` to index every PDF |
| `RPS_SCHOLARLY_MIN_SCORE` | `2` | Scholarly score a doc needs to be kept (strong signals score 2, supporting 1; lower = more permissive) |
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

1. **Walk** the directory for `*.pdf` files, skipping macOS `._*` resource‑fork
   files and recycle‑bin / system folders (`.Trashes`, `$RECYCLE.BIN`, …).
2. **Extract** structured fields from the first few pages plus the **full body
   text** (in parallel worker processes). Text is normalised so it matches the
   way it reads: ligatures are expanded ("ﬁ" → "fi"), hyphen variants unified,
   and words split across line breaks rejoined ("learn‑/ing" → "learning").
   Titles come from the PDF's metadata when it is printed on page 1, otherwise
   from the largest text on page 1; the year comes from the arXiv stamp,
   publication/copyright lines or proceedings headers before any other number.
3. **Classify** each document: strong evidence (DOI, arXiv id, a References
   heading, or an official issuer/report type on the cover such as "Bank of
   England Staff Working Paper" or "draft‑ietf‑…") and supporting evidence
   (abstract, section headings, citations, reputable publisher/venue/institution)
   are scored. Genuine papers/reports are kept; the rest are hidden from results
   and listed under **Hidden…**, where you can override the decision. PDFs with no
   extractable text (scanned or damaged) are reported separately so they can be
   OCR'd, never silently treated as "no match".
4. **Store** them in SQLite; an FTS5 virtual table + triggers keep the full‑text
   index in sync. Small columns are stored before the large text so reading a
   result never walks through a 200 KB body; text beyond `RPS_FULLTEXT_CHARS` is
   kept zlib‑compressed for exact collection.
5. **Search** ranks matches on the FTS index alone, then fetches only the best
   rows, on a background thread (typing never waits on the database). Papers containing
   *all* query terms rank above papers containing only some; within each group,
   candidates are re‑ranked with word‑start and fuzzy matching on the short,
   high‑signal fields. Accented terms work as typed or without accents.

**Exact collection** is separate and stricter: FTS only narrows candidates, then a
whole‑word/phrase regex decides matches (zero false positives), a paper must match
*every* keyword, non‑papers are excluded, and content‑identical files already in
the destination are skipped. Papers longer than `RPS_FULLTEXT_CHARS` are checked
against their complete stored text, never just the indexed part, without
re‑reading the PDF. A copied paper that
shares a DOI/arXiv id with one already in the destination (e.g. a preprint and
its published version) is copied but flagged for you to review.

Indexes live in a local cache directory (`RPS_INDEX_DIR`), one file per scanned
folder, so read‑only folders and network shares work and your paper folders stay
untouched. Upgrading to a new index format triggers a **one‑time re‑index** of that
folder on next open. (Older versions wrote `.pdf_search_index.db*` files into the
scanned folders; those can be deleted.)

---

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests generate their own PDFs (ligature glyphs, multi‑line titles, arXiv
stamps, scanned and damaged files) and keep their indexes in a temporary
directory.

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
