import json

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader

from helpers import CFG, grid, prepare
from helpers import header_footer as hf
from tmp_pipeline.contracts import (
    BaseHandler,
    ElementUpdate,
    HandlerRegistry,
    HandlerResult,
    MissingHandlerError,
)
from tmp_pipeline.handlers.stubs import stub_registry
from tmp_pipeline.models import ElementStore, PageInfo
from tmp_pipeline.records import Evidence, Record, RecordStore, Stamp
from tmp_pipeline.router import decide, route
from tmp_pipeline.vocab import Flag, Route


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
    got = {round(im.bbox_pt[2] - im.bbox_pt[0]): res.decisions[im.element_id] for im in s.of_type("image")}
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
    assert tb.route == "TEXT" and tb.outcome == "sent"


# --- contract: isolation, purity, registry, ownership -------------------------------------------------------------

class _Boom(BaseHandler):
    route = Route.TEXT
    version = "t"

    def handle(self, ctx, element):
        raise RuntimeError("boom")


class _Counting(BaseHandler):
    version = "t"

    def __init__(self, route):
        self.route, self.seen, self.began, self.finished = Route(route), [], False, False

    def begin(self, ctx):
        self.began = True

    def handle(self, ctx, element):
        self.seen.append(element.element_id)
        return HandlerResult(
            records=[Record("note", element.page, Stamp(str(self.route), "t"), evidence=[Evidence.of(element)])],
            element_updates={element.element_id: ElementUpdate(add_flags=(Flag.UNVERIFIED,), metadata={"seen": 1})})

    def finish(self, ctx):
        self.finished = True
        return HandlerResult()


def _form_page(make_pdf, name="x.pdf"):
    def page(c, w, h):
        hf(c, w, h)
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Free paragraph of text on the form page")
        grid(c, 100, 200, 3, 3)
    return prepare(make_pdf(name, [page, page, page]))


def _registry(text_handler):
    reg = stub_registry()
    reg.replace(text_handler)
    return reg


def test_handler_failure_is_isolated_logged_and_does_not_lose_the_log(make_pdf, tmp_path):
    s = _form_page(make_pdf)
    log = tmp_path / "router.log"
    res = route(s, CFG, log, _registry(_Boom()))
    texts = [e for e in s.elements.values() if e.route == "TEXT"]
    assert texts and all(e.outcome == "failed" for e in texts)
    assert len(res.failures) == len(texts) and "RuntimeError: boom" in res.failures[0]["error"]
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(s.elements)                                    # every element still has its line
    assert {x["outcome"] for x in lines if x["error"]} == {"failed"}
    assert res.coverage(s)["coverage"] == 1.0 and res.coverage(s)["failed"] == len(texts)


def test_handler_results_become_records_flags_and_metadata(make_pdf):
    s = _form_page(make_pdf)
    h, records = _Counting("TEXT"), RecordStore(s.doc_id)
    route(s, CFG, registry=_registry(h), records=records)
    assert h.began and h.finished and h.seen
    assert len(records) == len(h.seen) and all(records.records_for(i) for i in h.seen)
    e = s.elements[h.seen[0]]
    assert Flag.UNVERIFIED in e.flags and e.metadata["seen"] == 1


def test_missing_handler_fails_before_anything_runs(make_pdf):
    s = _form_page(make_pdf)
    reg = HandlerRegistry([_Counting("TEXT")])                              # no TABLE handler
    with pytest.raises(MissingHandlerError, match="TABLE"):
        route(s, CFG, registry=reg)
    assert all(e.outcome is None for e in s.elements.values())               # nothing was applied or run


def test_unused_routes_need_no_handler(make_pdf):
    def page(c, w, h):
        hf(c, w, h)
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Only text here")
    s = prepare(make_pdf("t.pdf", [page, page, page]))
    route(s, CFG, registry=HandlerRegistry([_Counting("TEXT")]))


def test_decide_does_not_touch_the_store(make_pdf):
    s = _form_page(make_pdf)
    before = [(e.element_id, e.flags[:], e.parent_id, e.route, e.outcome) for e in s.elements.values()]
    d1, d2 = decide(s, CFG), decide(s, CFG)
    assert d1 == d2 and len(d1) == len(s.elements)
    assert before == [(e.element_id, e.flags[:], e.parent_id, e.route, e.outcome) for e in s.elements.values()]


def test_registry_rejects_duplicate_route_and_unknown_route():
    reg = HandlerRegistry([_Counting("TEXT")])
    with pytest.raises(ValueError, match="already"):
        reg.register(_Counting("TEXT"))
    with pytest.raises(MissingHandlerError):
        reg.get("TABLE")


def test_checkbox_on_a_diagram_sheet_belongs_to_the_sheet_not_the_checkbox_handler(make_pdf):
    s = ElementStore("d")
    sheet = s.add(1, (842, 595), "diagram", "diagram_sheet", [0, 0, 842, 595])
    cb = s.add(1, (842, 595), "diagram", "checkbox", [10, 10, 20, 20], metadata={"state": "ticked"})
    s.pages[1] = PageInfo(width_pt=842, height_pt=595)
    d = decide(s, CFG)
    assert d[cb.element_id].route == "DIAGRAM" and d[cb.element_id].claimed_by == sheet.element_id
