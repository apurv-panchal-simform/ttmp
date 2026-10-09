import json

from PIL import Image
from reportlab.lib.utils import ImageReader

from tmp_pipeline.config import load_config
from tmp_pipeline.furniture import apply_furniture
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.to_elements import build_elements
from tmp_pipeline.router import route
from tmp_pipeline.zones import assign_zones

CFG = load_config()


def prepare(pdf):
    s = build_elements(read_layers(pdf), "sha256:t", CFG)
    assign_zones(s, CFG)
    apply_furniture(s, CFG)
    return s


def hf(c, w, h, n=1):
    c.setFont("Helvetica", 8)
    c.drawString(60, h - 30, "TMP Reference KTS 26-1581")
    c.drawString(60, 25, f"New Zealand Guide to Temporary Traffic Management 1. TMP Planning Form Page {n}")


def grid(c, x, y, cols, rows, cw=60, rh=20):
    c.setLineWidth(0.5)
    for i in range(rows + 1):
        c.line(x, y + i * rh, x + cols * cw, y + i * rh)
    for j in range(cols + 1):
        c.line(x + j * cw, y, x + j * cw, y + rows * rh)


def test_every_element_has_exactly_one_outcome_and_one_log_line(make_pdf, tmp_path):
    def page(c, w, h):
        hf(c, w, h)
        grid(c, 100, 200, 3, 3)
        c.setFont("Helvetica", 8)
        c.drawString(105, 250, "Road name")
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Free paragraph of text on the form page")
        c.acroForm.checkbox(name="cb", x=400, y=300, size=12, checked=True)
    s = prepare(make_pdf("r.pdf", [page, page, page]))
    res = route(s, CFG, tmp_path / "router.log")
    cov = res.coverage(s)
    assert cov["coverage"] == 1.0 and not cov["missing"] and not cov["duplicated"] and not cov["unknown"]
    lines = [json.loads(l) for l in (tmp_path / "router.log").read_text().splitlines()]
    assert len(lines) == len(s.elements)
    assert {"doc_id", "run_id", "router_version", "element", "page", "type", "route", "rule", "triage", "outcome"} <= set(lines[0])


def test_table_claims_its_text_and_checkbox_goes_to_checkbox_handler(make_pdf):
    def page(c, w, h):
        hf(c, w, h)
        grid(c, 100, 200, 3, 3)
        c.setFont("Helvetica", 8)
        c.drawString(105, 250, "Road name")
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Free paragraph of text on the form page")
        c.acroForm.checkbox(name="cb", x=400, y=300, size=12, checked=True)
    s = prepare(make_pdf("r.pdf", [page, page, page]))
    res = route(s, CFG)
    d = res.decisions
    (table,) = [e for e in s.of_type("table", page=1)]
    inner = [e for e in s.of_type("text_block", page=1) if e.parent_id == table.element_id]
    assert inner and all(d[e.element_id].outcome == "claimed" and d[e.element_id].claimed_by == table.element_id for e in inner)
    assert d[table.element_id].route == "TABLE"
    free = [e for e in s.of_type("text_block", page=1) if e.parent_id is None]
    assert [d[e.element_id].route for e in free] == ["TEXT"]
    (cb,) = s.of_type("checkbox", page=1)
    assert d[cb.element_id].route == "CHECKBOX"
    assert all(d[e.element_id].route == "METADATA" for e in s.of_type("page_furniture", page=1))


def test_logo_repeated_in_the_same_spot_is_decorative_and_skipped(make_pdf):
    logo = Image.new("RGB", (30, 10), (10, 80, 120))

    def page(c, w, h):
        hf(c, w, h)
        c.drawImage(ImageReader(logo), 700, 540, width=90, height=30)
        c.drawString(72, 450, "Free paragraph of text on the form page")
    s = prepare(make_pdf("l.pdf", [page, page, page]))
    res = route(s, CFG)
    imgs = s.of_type("image", page=1)
    assert len(imgs) == 1
    d = res.decisions[imgs[0].element_id]
    assert (d.route, d.triage, d.outcome, d.rule) == ("DECORATIVE", "decorative", "skipped", "figure.repeats_on_most_pages")
    assert "decorative" in imgs[0].flags


def test_large_figure_is_informative_and_unclear_one_needs_x1(make_pdf):
    big, mid = Image.new("RGB", (80, 60), (200, 200, 200)), Image.new("RGB", (40, 40), (100, 100, 200))

    def one(c, w, h):
        hf(c, w, h)
        c.drawImage(ImageReader(big), 100, 150, width=400, height=300)       # ~ 24% of the page
        c.drawImage(ImageReader(mid), 600, 100, width=120, height=100)       # ~ 2.4% -> small, no text
        c.drawImage(ImageReader(mid), 600, 300, width=170, height=130)       # ~ 4.4% -> unclear
    s = prepare(make_pdf("f.pdf", [one]))
    res = route(s, CFG)
    got = {round((im.bbox_pt[2] - im.bbox_pt[0])): res.decisions[im.element_id] for im in s.of_type("image")}
    assert got[400].triage == "informative" and got[400].route == "FIGURE" and got[400].outcome == "sent"
    assert got[120].triage == "decorative"
    assert got[170].triage == "needs_x1" and got[170].route == "FIGURE"       # unclear: treated as informative


def test_unclassified_page_content_is_leftover_and_flagged(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Some text with no recognisable cue at all here")
    s = prepare(make_pdf("u.pdf", [page]))
    res = route(s, CFG)
    (tb,) = s.of_type("text_block")
    assert res.decisions[tb.element_id].route == "LEFTOVER" and "leftover" in tb.flags


def test_generic_standard_text_is_flagged_generic(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "FH.M1 where work cannot occur near a pedestrian crossing")
    s = prepare(make_pdf("g.pdf", [page]))
    route(s, CFG)
    (tb,) = s.of_type("text_block")
    assert "generic" in tb.flags


def test_every_element_in_a_generic_standard_page_is_flagged_generic(make_pdf):
    def page(c, w, h):
        grid(c, 100, 200, 3, 3)
        c.setFont("Helvetica", 8)
        c.drawString(105, 250, "FH.M1 inside a table cell")
    s = prepare(make_pdf("g.pdf", [page]))
    route(s, CFG)
    assert s.elements and all("generic" in e.flags for e in s.elements.values())


def test_route_and_triage_are_stored_on_the_elements_for_later_stages(make_pdf):
    def page(c, w, h):
        hf(c, w, h)
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Free paragraph of text on the form page")
    s = prepare(make_pdf("m.pdf", [page, page, page]))
    route(s, CFG)
    (tb,) = [e for e in s.of_type("text_block", page=1)]
    assert tb.metadata["route"] == "TEXT" and tb.metadata["router_outcome"] == "sent"
