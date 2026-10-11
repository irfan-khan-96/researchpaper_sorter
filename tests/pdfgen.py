"""Tiny PDF writer for tests: text lines with font sizes, an optional rotated
margin stamp (like arXiv's), and an optional /Info metadata dictionary.

Text uses the standard Helvetica font, so byte 0xAE renders the "fi" ligature
glyph and 0xAF the "fl" glyph (Adobe StandardEncoding).
"""


def _escape(text: str) -> bytes:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)").encode("latin-1")


def make_pdf(path, pages, info=None, stamp=None):
    """
    pages: list of pages; each page is a list of ``(font_size, text)`` lines,
           top-down, or ``(font_size, text, x)`` to place a line at column x.
    stamp: text drawn rotated 90 degrees in the left margin of page 1, at 20pt.
    info:  dict for the /Info dictionary, e.g. {"Title": ..., "CreationDate": ...}.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = add(b"")  # filled in below
    page_ids = []
    for page_number, lines in enumerate(pages):
        ops = bytearray()
        y_by_column: dict[int, int] = {}
        for line in lines:
            size, text = line[0], line[1]
            x = line[2] if len(line) > 2 else 72
            y = y_by_column.get(x, 740)
            ops += b"BT /F1 %d Tf 1 0 0 1 %d %d Tm (" % (size, x, y) + _escape(text) + b") Tj ET\n"
            y_by_column[x] = y - int(size * 1.4)
        if stamp and page_number == 0:
            ops += b"BT /F1 20 Tf 0 1 -1 0 40 200 Tm (" + _escape(stamp) + b") Tj ET\n"
        content_id = add(b"<< /Length %d >>\nstream\n" % len(ops) + bytes(ops) + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
            b" /Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, content_id, font_id)
        ))
    kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids)
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info_id = None
    if info:
        entries = b" ".join(b"/" + key.encode() + b" (" + _escape(value) + b")" for key, value in info.items())
        info_id = add(b"<< " + entries + b" >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    trailer = b"/Size %d /Root %d 0 R" % (len(objects) + 1, catalog_id)
    if info_id:
        trailer += b" /Info %d 0 R" % info_id
    out += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF" % xref
    with open(path, "wb") as handle:
        handle.write(out)


# A realistic first page: running header, two-line title, authors, abstract with
# a line-break hyphen, keywords, a numbered first section, and body text.
BODY_LINES = [(10, f"Body sentence {i} describes the method in plain words.") for i in range(40)]


def paper_page(title=("Graph Neural Networks for", "Molecule Design"), extra=()):
    return [
        (9, "Journal of Testing 12 (2020) 1-10"),
        *[(20, line) for line in title],
        (11, "Jane Doe, John Roe"),
        (10, "Abstract"),
        (10, "We study deep learn-"),
        (10, "ing on graphs. Deep learning helps."),
        (10, "Keywords: graphs; molecules"),
        (10, "1 Introduction"),
        *extra,
        *BODY_LINES,
    ]
