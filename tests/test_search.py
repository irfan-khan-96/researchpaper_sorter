"""Interactive search: correctness, ranking, speed, and the background search thread."""
import os
import random
import string
import threading
import time

import pytest

import research_paper_search as rps

FIELDS = ("title", "authors", "year", "abstract", "keywords", "fulltext", "preview", "doi", "arxiv_id")


def add(conn, folder, name, **fields):
    row = dict.fromkeys(FIELDS, "")
    row.update(fields)
    rps.upsert_paper(conn, os.path.join(folder, name), 1.0, 10, row)


@pytest.fixture
def db(tmp_path):
    conn = rps.open_db(rps.get_db_path(str(tmp_path)))
    yield conn, str(tmp_path)
    conn.close()


def test_index_lives_outside_the_scanned_folder(tmp_path):
    path = rps.get_db_path(str(tmp_path))
    assert os.path.dirname(path) == rps.INDEX_DIR
    assert path == rps.get_db_path(str(tmp_path).upper() if os.name == "nt" else str(tmp_path))


def test_title_and_body_only_matches(db):
    conn, folder = db
    add(conn, folder, "a.pdf", title="Deep Learning for Protein Folding", year="2021",
        abstract="We predict protein structure.")
    add(conn, folder, "b.pdf", title="A Study of Widgets", year="2020",
        fulltext="Methodology. The flrbxdataset was used.", preview="Methodology.")
    conn.commit()
    assert "Protein Folding" in rps.search(conn, "protein folding")[0]["title"]
    assert rps.search(conn, "flrbxdataset")[0]["title"] == "A Study of Widgets"
    assert [r["year"] for r in rps.search(conn, "protein", year_filter="2021-2022")] == ["2021"]
    assert rps.search(conn, "protein", year_filter="1999") == []


def test_papers_with_all_terms_rank_first(db):
    conn, folder = db
    add(conn, folder, "one.pdf", title="Graph graph graph graph methods", fulltext="graph " * 50)
    add(conn, folder, "both.pdf", title="Notes", fulltext="a graph kernel approach")
    conn.commit()
    results = rps.search(conn, "graph kernel")
    assert results[0]["title"] == "Notes"
    assert {r["title"] for r in results} == {"Notes", "Graph graph graph graph methods"}


def test_accented_query_terms_stay_whole(db):
    conn, folder = db
    assert rps._fts_tokens(["schrödinger"]) == ['"schrödinger"*']
    add(conn, folder, "q.pdf", title="On the Schrödinger equation")
    add(conn, folder, "d.pdf", title="Dinger lake survey")
    conn.commit()
    titles = [r["title"] for r in rps.search(conn, "schrödinger")]
    assert titles == ["On the Schrödinger equation"]
    assert rps.search(conn, "schrodinger")  # diacritics folded like the index


def test_ranking_bonus_needs_a_word_start():
    attention = {"title": "Attention models", "keywords": "", "abstract": "", "authors": "", "preview": ""}
    ion = dict(attention, title="Ion channel models")
    assert rps.score_paper(["ion"], ion) > rps.score_paper(["ion"], attention)
    assert rps.score_paper(["atten"], attention) >= 500  # prefix of a word still counts


def test_no_match_search_is_fast_and_fallback_still_works(tmp_path):
    random.seed(1)
    vocab = ["".join(random.choices(string.ascii_lowercase, k=random.randint(4, 10))) for _ in range(5000)]
    conn = rps.open_db(rps.get_db_path(str(tmp_path)))
    for i in range(500):
        body = " ".join(random.choices(vocab, k=4000))
        add(conn, str(tmp_path), f"p{i}.pdf", title=" ".join(random.choices(vocab, k=8)),
            fulltext=body, preview=body[:4000], year="2020")
    conn.commit()
    start = time.perf_counter()
    assert rps.search(conn, "zzqxw") == []
    assert time.perf_counter() - start < 0.1
    assert rps.search(conn, vocab[5])
    saved = rps.FTS5_AVAILABLE
    rps.FTS5_AVAILABLE = False
    try:
        assert rps.search(conn, vocab[5]), "substring fallback without FTS5"
    finally:
        rps.FTS5_AVAILABLE = saved
    conn.close()


def test_search_worker_delivers_newest_result_off_thread(db):
    conn, folder = db
    add(conn, folder, "a.pdf", title="Alpha particles")
    add(conn, folder, "b.pdf", title="Beta decay")
    conn.commit()
    db_path = rps.get_db_path(folder)

    received = []
    done = threading.Event()

    def on_result(seq, results, error):
        received.append((seq, threading.current_thread().name, results, error))
        if seq == 3:
            done.set()

    worker = rps.SearchWorker(on_result)
    worker.start()
    for seq, query in enumerate(("alp", "alpha", "beta"), start=1):
        worker.submit(db_path, query, None, seq)
    assert done.wait(5)
    worker.stop()
    worker.join(5)
    seq, thread_name, results, error = received[-1]
    assert error is None and thread_name == "search"
    assert [r["title"] for r in results] == ["Beta decay"]
    assert all(r[0] <= 3 for r in received)
