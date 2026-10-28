"""Text extraction and field accuracy: normalisation, title, year, abstract, classification."""
import time

import pytest

import research_paper_search as rps
from pdfgen import make_pdf, paper_page


# --- normalisation ---------------------------------------------------------------

def test_normalize_text_expands_ligatures_and_hyphen_variants():
    assert rps.normalize_text("classiﬁcation of ﬂow") == "classification of flow"
    assert rps.normalize_text("self‐supervised") == "self-supervised"
    assert rps.normalize_text("learn­ing") == "learning"


def test_line_break_hyphens_repaired_only_with_evidence():
    pages = ["We study deep learn-\ning here.", "Deep learning works. A self-\nsupervised method."]
    front, body, truncated = rps._accumulate_pages(iter(pages), 4, 0)
    assert "learning here" in body and "learn-" not in body
    assert "self-supervised" in body          # no "selfsupervised" elsewhere -> genuine hyphen
    assert "deep learning here" in front[0].lower()
    assert not truncated
    _, across_pages, _ = rps._accumulate_pages(iter(["a new repre-", "sentation; the representation"]), 4, 0)
    assert "new representation" in across_pages
    _, covid, _ = rps._accumulate_pages(iter(["cases of COVID-\n19 rose"]), 4, 0)
    assert "COVID-19" in covid


def test_truncation_is_reported():
    _, body, truncated = rps._accumulate_pages(iter(["x" * 100, "y" * 100]), 1, 150)
    assert truncated and len(body) <= 151


def test_ligature_glyphs_in_a_real_pdf(tmp_path):
    path = tmp_path / "lig.pdf"
    make_pdf(path, [[(10, "Image classi\xaecation and e\xaecient \xafow control.")] + [(10, "filler " * 10)] * 5])
    body = rps._clean(rps._extract_pdf_content(str(path)).body)
    assert "classification" in body and "flow" in body


# --- title, authors, year ----------------------------------------------------------

def test_title_from_font_size_not_running_header_or_stamp(tmp_path):
    path = tmp_path / "a.pdf"
    make_pdf(path, [paper_page()], stamp="arXiv:2010.12345v2 [cs.LG] 5 Oct 2020")
    fields = rps._extract_fields(str(path))
    assert fields["title"] == "Graph Neural Networks for Molecule Design"
    assert fields["authors"].startswith("Jane Doe")
    assert fields["year"] == "2020"              # stamp date, not "2010" from the id
    assert "deep learning on graphs" in fields["fulltext"].lower()


@pytest.mark.parametrize("meta, expected", [
    ("Graph Neural Networks for Molecule Design", "Graph Neural Networks for Molecule Design"),
    ("Microsoft Word - draft3.docx", "Graph Neural Networks for Molecule Design"),
    ("Journal of Testing", "Graph Neural Networks for Molecule Design"),
    ("paper_final.pdf", "Graph Neural Networks for Molecule Design"),
])
def test_metadata_title_used_only_when_sane(tmp_path, meta, expected):
    path = tmp_path / "m.pdf"
    make_pdf(path, [paper_page()], info={"Title": meta})
    assert rps._extract_fields(str(path))["title"] == expected


@pytest.mark.parametrize("page0, meta_year, expected", [
    ("arXiv:2010.12345v2 [cs.LG] 5 Oct 2020\nSome text from 2012", None, "2020"),
    ("Received: 3 May 2019 / Accepted: 2 January 2020 / Published online: 15 February 2020", None, "2020"),
    ("We cite work from 2015.\n© 2021 Elsevier Ltd. All rights reserved.", None, "2021"),
    ("34th Conference on Neural Information Processing Systems (NeurIPS 2020), Vancouver", None, "2020"),
    ("A widely accepted model since 1998. Results from 2012 and 2016.", None, "2016"),
    ("We compare (a) X, (b) Y and (c) Z, first seen in 2010; see also 2014.", None, "2014"),
    ("doi:10.1016/j.cell.2015.01.001 targets for 2050, studied in 2013", None, "2013"),
    ("arXiv:2103.01234 preprint", None, "2021"),
    ("No years at all here.", 2019, "2019"),
    ("No years at all here.", None, ""),
])
def test_year(page0, meta_year, expected):
    assert rps._guess_year(page0, page0, meta_year) == expected


# --- abstract and keywords -----------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Title\nAbstract\nWe present X.\nIt works well.\n1 Introduction\nDeep learning...",
     "We present X.\nIt works well."),
    ("Abstract—We study Y on graphs.\nMore detail.\nIndex Terms—graphs\nI. INTRODUCTION\nText",
     "We study Y on graphs.\nMore detail."),
    ("Abstract\nBackground: Diabetes is common.\nMethods: A trial.\nResults: It helped.\n"
     "Keywords: diabetes\n1. Introduction\nX",
     "Background: Diabetes is common.\nMethods: A trial.\nResults: It helped."),
    ("Abstract\nWe use 2 models and\n3D data in 2019.\n1 Introduction\nX",
     "We use 2 models and\n3D data in 2019."),
])
def test_abstract_stops_at_next_section(text, expected):
    assert rps._extract_block(text, rps._ABSTRACT_HEAD_RE, 1200) == expected


def test_keywords_limited_to_their_lines():
    text = "Keywords: graphs; molecules\n1 Introduction\nLong body text follows here."
    assert rps._extract_block(text, rps._KEYWORDS_HEAD_RE, 400, max_lines=2) == "graphs; molecules"


def test_doi_drops_trailing_punctuation(tmp_path):
    path = tmp_path / "d.pdf"
    make_pdf(path, [paper_page(extra=[(10, "See doi:10.1234/abc.def.")])])
    assert rps._extract_fields(str(path))["doi"] == "10.1234/abc.def"


# --- classification --------------------------------------------------------------------

def test_invoice_rejected_despite_lookalike_words():
    invoice = ("INVOICE 4411\nThank you for your message.\nPayment methods: card or transfer.\n"
               "Signature: ________\nWe share your concern about delays.\nTotal due: 120.00")
    assert not rps.classify_document({"title": "Invoice"}, raw_text=invoice)[0]


def test_paper_and_report_accepted():
    paper = ("Abstract\nWe propose X [1], [2].\n1 Introduction\nPrior work [3] [4] [5].\n"
             "2 Methods\nWe do Y.\n3 Results\nIt works.\nReferences\n[1] A. Smith et al. 2019")
    ok, reason = rps.classify_document({"abstract": "We propose X."}, raw_text=paper)
    assert ok and "references section" in reason
    report = ("World Health Organization\nGlobal report on vaccines\nExecutive Summary\n"
              "Coverage improved.\nIntroduction\nThis report reviews coverage.\nConclusions\nMore work.")
    assert rps.classify_document({}, raw_text=report)[0]


def test_section_words_in_prose_are_not_headings():
    prose = ("results in a significant reduction of the overall error rate for every class\n"
             "methods vary widely in practice and in theory too")
    assert not rps.classify_document({}, raw_text=prose)[0]


def test_scanned_and_broken_pdfs_flagged_unreadable(tmp_path):
    scan = tmp_path / "scan.pdf"
    make_pdf(scan, [[]])
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 garbage")
    for path, reason in ((scan, "no text layer"), (broken, "unreadable")):
        fields = rps._extract_fields(str(path))
        assert fields["has_text"] == 0 and fields["is_scholarly"] == 0
        assert fields["doc_reason"].startswith(reason)


# --- speed -----------------------------------------------------------------------------------

@pytest.mark.skipif(not (rps.PDFIUM_OK and rps.PDFPLUMBER_OK), reason="needs pdfium and pdfplumber")
def test_pdfium_much_faster_than_pdfplumber(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, [[(10, f"Page {n} line {i}: graph neural networks for molecules.") for i in range(45)]
                    for n in range(10)])
    timings = {}
    for name, read in (("pdfium", rps._read_pdfium), ("pdfplumber", rps._read_pdfplumber)):
        start = time.perf_counter()
        result = read(str(path), 4, 200000)
        timings[name] = time.perf_counter() - start
        assert "graph neural networks" in result.body
    assert timings["pdfplumber"] > 3 * timings["pdfium"], timings


@pytest.mark.parametrize("cover", [
    "PROJECT ACACIA - CONSULTATION PAPER | NOVEMBER 2024 1\nExploring digital money\nIntroduction",
    "Secure Asset Transfer Protocol (SATP) Core (draft-ietf-satp-core-16)\nAuthors: M. H.",
    "Bank of England\nStaff Working Paper No. 1,000\nInnovations in wholesale settlement",
    "BIS Innovation Hub\nProject mBridge: an update\nOctober 2023",
])
def test_institutional_reports_accepted_from_cover(cover):
    ok, reason = rps.classify_document({}, raw_text=cover + "\n" + "Body text. " * 200)
    assert ok and reason.startswith("issued by"), reason


def test_issuer_deep_in_body_or_cbdc_topic_is_not_enough():
    lecture = "Wholesale Central Bank Digital Currency\nLecture 3\n" + "Slide text. " * 300
    assert not rps.classify_document({}, raw_text=lecture)[0]
    mention = "Course notes\n" + "Filler. " * 400 + "\nAs the Bank of England noted, ..."
    assert not rps.classify_document({}, raw_text=mention)[0]


def test_long_text_split_into_index_head_and_stored_tail(tmp_path, monkeypatch):
    path = tmp_path / "long.pdf"
    make_pdf(path, [paper_page()] + [[(10, f"Line {i} on page {p}.") for i in range(45)] for p in range(4)])
    monkeypatch.setattr(rps, "FULLTEXT_CHARS", 1000)
    fields = rps._extract_fields(str(path))
    whole = rps._clean(rps._extract_pdf_content(str(path), char_limit=0).body)
    assert len(fields["fulltext"]) == 1000 and fields["truncated"] == 0
    assert fields["fulltext"] + rps.zlib.decompress(fields["tail"]).decode() == whole


def test_bare_ietf_mention_is_not_an_issuer():
    slides = "TLS and SSH\nLecture 4, Network Security\nStandardised by the IETF.\n" + "Slide. " * 300
    assert not rps.classify_document({}, raw_text=slides)[0]
    draft = "Internet-Draft                                   Gateway Recovery\nIntended status: Informational\n"
    assert rps.classify_document({}, raw_text=draft + "Body. " * 300)[0]
