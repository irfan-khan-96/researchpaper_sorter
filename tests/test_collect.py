"""Exact-match collection: zero false positives, completeness, duplicates, reporting."""
import os

import research_paper_search as rps
from pdfgen import BODY_LINES, make_pdf


def paper(title, *lines, doi=None):
    return [[(20, title), (11, "A. Author"), (10, "Abstract"), *[(10, line) for line in lines],
             (10, "1 Introduction"), (10, "Text [1] [2]."), (10, "2 Results"), (10, "It works."),
             (10, "References"), (10, "[1] B. Other et al. 2019"),
             *([(10, f"doi:{doi}")] if doi else []), *BODY_LINES]]


def test_keyword_patterns_are_whole_word():
    ion = rps.compile_keyword_patterns(["ion"], False)[0]
    assert ion.search("an ion of gold") and not ion.search("ionization") and not ion.search("champion")
    phrase = rps.compile_keyword_patterns(["machine learning"], False)[0]
    assert phrase.search("MACHINE   learning") and not phrase.search("machine learnings")
    assert rps.compile_keyword_patterns(["Cancer"], True)[0].search("cancer") is None
    assert rps.compile_keyword_patterns(["classiﬁcation"], False)[0].search("image classification")
    assert rps.parse_keywords("a, b\n a ; c") == ["a", "b", "c"]


def test_phrase_never_stitched_across_fields():
    pats = rps.compile_keyword_patterns(["machine learning"], False)
    stitched = {"title": "Advances in Machine", "authors": "Learning Systems Group",
                "abstract": "", "keywords": "", "fulltext": "nothing relevant"}
    assert not rps.paper_matches(stitched, pats)
    assert rps.paper_matches(dict(stitched, fulltext="we apply machine learning"), pats)


def test_collect_end_to_end(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    make_pdf(src / "paper.pdf", paper("Quantum Widget Entanglement", "We study quantumfoobar effects."))
    make_pdf(src / "partial.pdf", paper("Quantum Widgets Again", "We study quantumfoobars only."))
    make_pdf(src / "invoice.pdf", [[(10, "Invoice for quantumfoobar parts")] +
                                   [(10, f"Line item {i}: quantity and price.") for i in range(10)]])
    make_pdf(src / "scan.pdf", [[]])
    (src / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")

    stats = rps.aggregate_matches(str(src), str(dest), ["quantumfoobar"], workers=2)
    assert os.listdir(dest) == ["paper.pdf"]          # "quantumfoobars" is not a whole-word match
    assert (stats.copied, stats.skipped_nonpaper, stats.unreadable) == (1, 1, 2)

    again = rps.aggregate_matches(str(src), str(dest), ["quantumfoobar"], workers=1)
    assert (again.copied, again.skipped_existing) == (0, 1)
    assert os.listdir(dest) == ["paper.pdf"]


def test_same_doi_different_file_is_copied_but_flagged(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    make_pdf(src / "preprint.pdf", paper("Catalysis With Zeolites", "zeolitex catalysis.", doi="10.5555/zx.1"))
    make_pdf(src / "published.pdf", paper("Catalysis With Zeolites", "zeolitex catalysis, final.", doi="10.5555/zx.1"))
    stats = rps.aggregate_matches(str(src), str(dest), ["zeolitex"], workers=1)
    assert stats.copied == 2
    assert len(stats.possible_duplicates) == 1 and "same DOI" in stats.possible_duplicates[0]


def test_text_beyond_the_index_cap_is_still_checked(tmp_path, monkeypatch):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    pages = paper("Long Report on Materials", "Short abstract.")
    pages += [[(10, f"Filler line {i} on page {p} about materials.") for i in range(45)] for p in range(5)]
    pages += [[(10, "The rare term zirconiumx appears only at the very end.")]]
    make_pdf(src / "long.pdf", pages)
    monkeypatch.setattr(rps, "FULLTEXT_CHARS", 3000)  # serial run, so workers see it too
    stats = rps.aggregate_matches(str(src), str(dest), ["zirconiumx"], workers=1)
    assert stats.copied == 1, stats


def test_copy_hashes_only_on_size_collision(tmp_path, monkeypatch):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    a, b, c = src / "a.pdf", src / "b.pdf", src / "c.pdf"
    a.write_bytes(b"%PDF-1.4 aaaa")
    b.write_bytes(b"%PDF-1.4 a longer file")
    c.write_bytes(b"%PDF-1.4 aaaa")                      # same bytes as a
    hashed = []
    real_hash = rps._hash_file
    monkeypatch.setattr(rps, "_hash_file", lambda path, *args: hashed.append(path) or real_hash(path, *args))
    copied, errors, skipped = rps._copy_into([str(a), str(b), str(c)], str(dest))
    assert [name for _, name in copied] == ["a.pdf", "b.pdf"] and skipped == 1 and errors == 0
    assert str(b) not in hashed                          # unique size: never hashed


def test_collect_never_rereads_pdfs_for_long_papers(tmp_path, monkeypatch):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    pages = paper("Long Report on Materials", "Short abstract.")
    pages += [[(10, f"Filler line {i} on page {p} about materials.") for i in range(45)] for p in range(5)]
    pages += [[(10, "The rare term hafniumx appears only at the very end.")]]
    make_pdf(src / "long.pdf", pages)
    monkeypatch.setattr(rps, "FULLTEXT_CHARS", 3000)
    rps.index_directory(str(src), rps.get_db_path(str(src)), workers=1)

    def no_pdf_access(*_args, **_kwargs):
        raise AssertionError("Collect re-read a PDF")
    monkeypatch.setattr(rps, "_extract_pdf_content", no_pdf_access)
    stats = rps.aggregate_matches(str(src), str(dest), ["hafniumx", "materials"], workers=1)
    assert stats.copied == 1, stats
