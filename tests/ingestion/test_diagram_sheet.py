"""The diagram stage on synthetic sheets: symbols, legend, notes, title block, regions, elements, handler record."""
from helpers import CFG, label, plate, prepare, sheet_page, speed_disc
from tmp_pipeline.ingestion.extract.diagrams.stage import read_diagrams
from tmp_pipeline.ingestion.extract.handlers.diagram import DiagramHandler, aliases, tmd_id
from tmp_pipeline.ingestion.vocab import ElementType, Flag, SymbolKind
from tmp_pipeline.records import Status


def _signs(c, w, h):
    plate(c, 200, 300, "TEMPORARY")
    speed_disc(c, 232, 318, 30)
    speed_disc(c, 262, 292, 50)
    label(c, 190, 340, "RP 0.615", w=34)
    plate(c, 400, 300, "WORKS END")
    plate(c, 450, 300, "")                                   # an orange plate with no text: a pictogram to caption
    label(c, 300, 400, "SUNRISE AVENUE - L1", w=70)
    label(c, 300, 380, "25 m", w=20)


def _read(make_pdf, pages=None):
    pdf = make_pdf("s.pdf", pages or [lambda c, w, h: sheet_page(c, w, h, extra=_signs)])
    store = prepare(pdf)
    return store, read_diagrams(store, pdf, CFG, None, render=False)


def test_sheet_is_found_as_a_diagram_with_title_block_fields(make_pdf):
    store, sheets = _read(make_pdf)
    s = sheets.get(1)
    assert s is not None and s.vector
    assert (s.sheet, s.rev, s.title) == ("04", "00", "STOP GO")
    assert s.title_block["tmp_number"] == "KTS 26-1581"
    assert [r.kind for r in s.regions if r.kind == "title_block"] == ["title_block"]


def test_legend_swatches_are_decoded_in_drawing_order(make_pdf):
    _, sheets = _read(make_pdf)
    assert [(e.swatch, e.meaning) for e in sheets.get(1).legend] == [("green", "Work Area"), ("yellow", "Safety Zone")]


def test_notes_are_split_at_bullets(make_pdf):
    _, sheets = _read(make_pdf)
    assert sheets.get(1).notes == ["1M minimum lateral safety zone", "10M Longitudinal safety zone and 30M taper length"]


def test_signs_are_grouped_classified_and_associated(make_pdf):
    _, sheets = _read(make_pdf)
    by_kind: dict[SymbolKind, list] = {}
    for s in sheets.get(1).symbols:
        by_kind.setdefault(s.kind, []).append(s)
    temp = by_kind[SymbolKind.TEMPORARY_SPEED_LIMIT]
    assert [(s.value, s.unit, s.rp, s.temporary) for s in temp] == [(30, "km/h", 0.615, True)]
    assert [s.value for s in by_kind[SymbolKind.SPEED_LIMIT]] == [50]
    assert [s.text for s in by_kind[SymbolKind.SUPPLEMENTARY_PLATE]] == ["TEMPORARY"]
    assert [s.text for s in by_kind[SymbolKind.END_OF_WORKS_SIGN]] == ["WORKS END"]
    assert by_kind[SymbolKind.RP_LABEL][0].value == 0.615
    road = by_kind[SymbolKind.ROAD_LABEL][0]
    assert (road.value, road.meaning) == ("SUNRISE AVENUE", "L1")
    dim = by_kind[SymbolKind.DIMENSION_LABEL][0]
    assert (dim.value, dim.unit) == (25, "m")
    assert sheets.get(1).roads == [{"name": "Sunrise Avenue", "level": "L1"}]


def test_a_sign_without_text_is_a_pictogram_that_needs_a_caption(make_pdf):
    store, sheets = _read(make_pdf)
    pics = [s for s in sheets.get(1).symbols if s.kind == SymbolKind.PICTOGRAM]
    assert len(pics) == 1 and pics[0].needs_caption
    el = store.elements[pics[0].id]
    assert el.type == ElementType.VECTOR_SYMBOL and Flag.NEEDS_CAPTION in el.flags


def test_legend_notes_and_title_block_are_not_symbols(make_pdf):
    _, sheets = _read(make_pdf)
    texts = " ".join(s.text for s in sheets.get(1).symbols)
    assert "Work Area" not in texts and "NOTES" not in texts and "PREPARED" not in texts and "KTS 26" not in texts


def test_elements_are_added_under_the_sheet_with_ids_the_symbols_carry(make_pdf):
    store, sheets = _read(make_pdf)
    sheet_el = store.of_type(ElementType.DIAGRAM_SHEET)[0]
    kinds = {e.type for e in store.on_page(1) if e.parent_id == sheet_el.element_id}
    assert {ElementType.VECTOR_SYMBOL, ElementType.LEGEND, ElementType.NOTES, ElementType.REGION} <= kinds
    for s in sheets.get(1).symbols:
        assert store.elements[s.id].metadata["kind"] == str(s.kind)
    rp = next(s for s in sheets.get(1).symbols if s.rp is not None)
    assert rp.rp_id in store.elements


def test_the_drawing_frame_the_table_finder_saw_becomes_a_region_not_a_table(make_pdf):
    def frame(c, w, h):
        c.setLineWidth(1)
        c.rect(14, 96, w - 28, h - 110)
        for x in (200, 400, 600):
            c.line(x, 96, x, h - 14)
        for y in (200, 300, 400):
            c.line(14, y, w - 14, y)
    pdf = make_pdf("f.pdf", [lambda c, w, h: sheet_page(c, w, h, extra=frame)])
    store = prepare(pdf)
    read_diagrams(store, pdf, CFG, None, render=False)
    assert not [t for t in store.of_type(ElementType.TABLE) if t.bbox_pt[2] - t.bbox_pt[0] > 700]


def test_a_reused_aerial_is_linked_between_sheets_and_a_different_one_is_not(make_pdf):
    pages = [lambda c, w, h: sheet_page(c, w, h, sheet="01", aerial_seed=1),
             lambda c, w, h: sheet_page(c, w, h, sheet="02", aerial_seed=1),
             lambda c, w, h: sheet_page(c, w, h, sheet="03", aerial_seed=7)]
    _, sheets = _read(make_pdf, pages)
    aerial = {p: next(i for i in sheets.get(p).images if i.role == "aerial") for p in (1, 2, 3)}
    assert aerial[1].reused_on == [2] and aerial[2].reused_on == [1] and aerial[3].reused_on == []


def test_the_handler_writes_one_record_in_the_plan_shape(make_pdf):
    store, sheets = _read(make_pdf)
    records, result = _route(store, sheets)
    assert not result.failures
    rec = records.of_kind("diagram_sheet")[0]
    assert rec.fields["tmd_id"].value == "TMD-04" and rec.fields["title"].value == "STOP GO"
    assert rec.fields["tmd_id"].method == "vector" and rec.fields["tmd_id"].status == Status.VERIFIED
    assert rec.data["aliases"][:2] == ["TMD 04: STOP GO", "TMD Sheet 04 Stop Go"]
    assert rec.data["legend"][0] == {"swatch": "green", "meaning": "Work Area"}
    assert rec.data["summary"] is None
    assert any(s["kind"] == "temporary_speed_limit" and s["value"] == 30 and s["rp"] == 0.615 for s in rec.data["symbols"])
    assert Flag.NEEDS_CAPTION in rec.flags


def test_tmd_ids_and_aliases():
    assert tmd_id("04") == "TMD-04" and tmd_id("x") is None and tmd_id(None) is None
    assert aliases("4", None) == ["TMD 04", "TMD4", "TMD-04", "TMD Sheet 04"]
    assert aliases(None, "STOP GO") == []


def _route(store, sheets):
    from tmp_pipeline.ingestion.extract.handlers.stubs import stub_registry
    from tmp_pipeline.ingestion.extract.router import route
    from tmp_pipeline.records import RecordStore
    reg = stub_registry()
    reg.replace(DiagramHandler())
    records = RecordStore(store.doc_id)
    return records, route(store, CFG, registry=reg, records=records, sheets=sheets)


def test_a_sheet_without_a_numeric_sheet_number_is_unverified(make_pdf):
    store, sheets = _read(make_pdf, [lambda c, w, h: sheet_page(c, w, h, sheet="A")])
    records, _ = _route(store, sheets)
    rec = records.of_kind("diagram_sheet")[0]
    assert rec.fields["tmd_id"].status == Status.UNVERIFIED and rec.status == Status.UNVERIFIED


def test_stage_off_leaves_the_sheet_with_no_model_and_a_note(make_pdf):
    store = prepare(make_pdf("s.pdf", [lambda c, w, h: sheet_page(c, w, h)]))
    records, res = _route(store, None)
    assert not res.failures and not records.of_kind("diagram_sheet")


def test_the_router_sends_the_sheet_and_the_handler_claims_what_is_drawn_on_it(make_pdf):
    from tmp_pipeline.ingestion.extract.router import decide
    from tmp_pipeline.ingestion.vocab import Outcome, Route
    store, _ = _read(make_pdf)
    decisions = decide(store, CFG)
    sheet = store.of_type(ElementType.DIAGRAM_SHEET)[0]
    assert (decisions[sheet.element_id].route, decisions[sheet.element_id].outcome) == (Route.DIAGRAM, Outcome.SENT)
    symbols = store.of_type(ElementType.VECTOR_SYMBOL)
    assert symbols and all(decisions[s.element_id].outcome == Outcome.CLAIMED
                           and decisions[s.element_id].claimed_by == sheet.element_id for s in symbols)
    assert not [d for d in decisions.values() if d.route == Route.LEFTOVER]
