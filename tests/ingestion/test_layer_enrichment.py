"""Layer data that handlers rely on: per-line typography, strikethrough, table edges and fills."""
from helpers import CFG, grid, prepare


def _blocks(s, page=1):
    return [e for e in s.of_type("text_block", page=page)]


def test_blocks_keep_per_line_size_and_weight_so_a_heading_can_be_found_inside_a_block(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(72, 450, "Spills")
        c.setFont("Helvetica", 9)
        c.drawString(72, 438, "In case of spills, STMS should suspend all works.")
    s = prepare(make_pdf("h.pdf", [page]))
    (b,) = _blocks(s)
    lines = b.representations["lines"]
    assert [ln["text"] for ln in lines] == ["Spills", "In case of spills, STMS should suspend all works."]
    assert [ln["bold"] for ln in lines] == [True, False]
    assert b.representations["text"] == "Spills\nIn case of spills, STMS should suspend all works."


def test_strikethrough_rule_through_text_marks_only_that_line(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Struck out sentence")
        c.drawString(72, 300, "Normal sentence")
        width = c.stringWidth("Struck out sentence", "Helvetica", 10)
        c.setLineWidth(0.5)
        c.line(72, 453.5, 72 + width, 453.5)
    s = prepare(make_pdf("s.pdf", [page]))
    struck = {ln["text"]: ln["struck"] for b in _blocks(s) for ln in b.representations["lines"]}
    assert struck == {"Struck out sentence": True, "Normal sentence": False}


def test_a_cell_border_longer_than_the_text_is_not_a_strikethrough(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(100, 450, "KTS 26-1581")
        c.setLineWidth(0.5)
        c.line(80, 453.5, 300, 453.5)               # border running well past the text on both sides
    s = prepare(make_pdf("b.pdf", [page]))
    assert not any(ln["struck"] for b in _blocks(s) for ln in b.representations["lines"])


def test_underline_below_the_text_is_not_a_strikethrough(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Underlined")
        width = c.stringWidth("Underlined", "Helvetica", 10)
        c.line(72, 448, 72 + width, 448)
    s = prepare(make_pdf("u.pdf", [page]))
    assert not any(ln["struck"] for b in _blocks(s) for ln in b.representations["lines"])


def test_table_elements_carry_ruling_segments_and_fills(make_pdf):
    def page(c, w, h):
        grid(c, 100, 200, 3, 2)
        c.setFillColorRGB(1, 0, 0)
        c.rect(100, 220, 60, 20, stroke=0, fill=1)
    s = prepare(make_pdf("t.pdf", [page]))
    (t,) = s.of_type("table")
    assert [[round(v) for v in r] for r in t.metadata["v_rules"]] == [[100, 200, 240], [160, 200, 240], [220, 200, 240], [280, 200, 240]]
    assert [[round(v) for v in r] for r in t.metadata["h_rules"]] == [[200, 100, 280], [220, 100, 280], [240, 100, 280]]
    assert t.metadata["fills"] == [{"bbox": [100.0, 220.0, 160.0, 240.0], "rgb": [1.0, 0.0, 0.0]}]
    assert CFG.text.strike_max_overhang_pt > 0
