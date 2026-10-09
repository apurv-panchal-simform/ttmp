from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from tmp_pipeline.config import load_config
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.ticks import detect_ticks

CFG = load_config()


def ticks(pdf):
    return detect_ticks(read_layers(pdf)[0], CFG)


def test_acroform_wins_over_everything_else(make_pdf):
    def page(c, w, h):
        c.acroForm.checkbox(name="a", x=100, y=100, size=15, checked=True)
        c.acroForm.checkbox(name="b", x=200, y=100, size=15, checked=False)
    ts = ticks(make_pdf("a.pdf", [page]))
    assert [(t.method, t.ticked, t.name) for t in ts] == [("acroform", True, "a"), ("acroform", False, "b")]


def test_vector_squares_ticked_when_a_path_lies_inside(make_pdf):
    def page(c, w, h):
        c.setLineWidth(0.8)
        c.rect(100, 100, 10, 10, stroke=1, fill=0)           # empty box
        c.rect(200, 100, 10, 10, stroke=1, fill=0)           # ticked box
        c.line(201.5, 105, 204, 101.5)
        c.line(204, 101.5, 208.5, 108.5)
    ts = sorted(ticks(make_pdf("v.pdf", [page])), key=lambda t: t.bbox[0])
    assert [(t.method, t.ticked) for t in ts] == [("vector_square", False), ("vector_square", True)]


def test_table_cells_are_not_mistaken_for_boxes(make_pdf):
    def page(c, w, h):
        c.rect(100, 100, 200, 60, stroke=1, fill=0)          # far too big for a box
    assert ticks(make_pdf("c.pdf", [page])) == []


def test_text_glyphs_are_read_when_the_font_has_them(make_pdf):
    import pytest
    from pathlib import Path
    font = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",) if Path(p).exists()), None)
    if font is None:
        pytest.skip("no TTF font with ballot-box glyphs")
    pdfmetrics.registerFont(TTFont("DejaVu", font))

    def page(c, w, h):
        c.setFont("DejaVu", 12)
        c.drawString(100, 300, "Pedestrians: ☒")
        c.drawString(100, 280, "Cyclists: ☐")
    ts = sorted(ticks(make_pdf("g.pdf", [page])), key=lambda t: -t.bbox[1])
    assert [(t.method, t.ticked) for t in ts] == [("glyph", True), ("glyph", False)]
