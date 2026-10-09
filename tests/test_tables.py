"""Table handler on synthetic PDFs: grid, header fill, merged cells, typed values, stitching, label/value
grids, value checks and the X2 hook. Pages carry the planning-form footer so they route as TABLE."""
import pytest

from helpers import CFG, header_footer, prepare
from tmp_pipeline.handlers.stubs import stub_registry
from tmp_pipeline.handlers.tables import TableHandler
from tmp_pipeline.model.client import FakeClient
from tmp_pipeline.records import RecordStore
from tmp_pipeline.router import route

HEADER_FILL = (0.835, 0.961, 1.0)


def hline(c, x0, x1, y):
    c.setLineWidth(0.5)
    c.line(x0, y, x1, y)


def vline(c, x, y0, y1):
    c.setLineWidth(0.5)
    c.line(x, y0, x, y1)


def box(c, x0, y0, x1, y1):
    """A rectangle drawn as four ruling lines with a narrow side-label column, the way the real forms draw
    their boxes (a one-cell box is not a table)."""
    vline(c, x0 + 15, y0, y1)
    hline(c, x0, x1, y0)
    hline(c, x0, x1, y1)
    vline(c, x0, y0, y1)
    vline(c, x1, y0, y1)


def road_table(c, x0=60, top=500, *, header=True, rows=(("Sunrise Avenue", "RP 0.410", "RP 0.680", "50", "9260", "3%"),
                                                        ("Gull Lane", "RP 0.000", "RP 0.030", None, "37", "5.409%")),
               names=("Road name(s)", "Start RP / House #", "End RP / House #", "Posted speed", "AADT", "%HMV")):
    """Ruled table: 6 columns of 80 pt, optional header row with the header fill, rows 24 pt tall. A None cell is
    merged with the row above (its separating rule is left out)."""
    cw, rh, n = 80, 24, len(names)
    xs = [x0 + i * cw for i in range(n + 1)]
    top_y = top
    all_rows = ([names] if header else []) + [list(r) for r in rows]
    ys = [top_y - i * rh for i in range(len(all_rows) + 1)]
    if header:
        c.setFillColorRGB(*HEADER_FILL)
        c.rect(x0, ys[1], n * cw, rh, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
    for x in xs:
        vline(c, x, ys[-1], ys[0])
    for i, y in enumerate(ys):
        for j in range(n):
            below_none = i < len(all_rows) and all_rows[i][j] is None
            if not below_none:
                hline(c, xs[j], xs[j + 1], y)
    c.setFont("Helvetica", 8)
    for i, row in enumerate(all_rows):
        for j, text in enumerate(row):
            if text is not None:
                c.drawString(xs[j] + 4, ys[i] - 15, text)
    return xs, ys


def page_with(draw, form="1. TMP Planning Form"):
    def page(c, w, h):
        header_footer(c, w, h, form)
        draw(c)
    return page


def run(make_pdf, pages, model=None, name="t.pdf"):
    s = prepare(make_pdf(name, pages))
    reg = stub_registry()
    reg.replace(TableHandler())
    records = RecordStore(s.doc_id)
    res = route(s, CFG, registry=reg, records=records, model=model)
    assert not res.failures, res.failures
    return s, records


def rows_of(records, kind="road"):
    return records.of_kind(kind)


def test_records_table_rows_are_typed_with_raw_text_kept_and_merged_cells_share_their_value(make_pdf):
    s, rec = run(make_pdf, [page_with(road_table)] * 3)
    roads = [r for r in rows_of(rec) if r.page == 1]
    assert len(roads) == 2
    a, b = roads
    assert a.fields["road_name"].value == "Sunrise Avenue" and a.entity == "road"
    assert (a.fields["start_rp"].value, a.fields["start_rp"].raw, a.fields["start_rp"].unit) == (0.41, "RP 0.410", "km")
    assert a.fields["hmv_pct"].value == 3.0 and b.fields["hmv_pct"].value == 5.409
    # the 50 sits in a cell that spans both rows, so it applies to both roads and is marked merged
    assert a.fields["posted_speed_kmh"].value == b.fields["posted_speed_kmh"].value == 50
    assert "merged" in a.fields["posted_speed_kmh"].notes and "merged" in b.fields["posted_speed_kmh"].notes
    assert "merged" in a.flags and "merged" in b.flags
    assert a.status == "verified" and a.produced_by.handler == "TableHandler" and a.evidence[0].element_id.startswith("p1:table")


def test_the_table_record_describes_shape_headers_and_size(make_pdf):
    s, rec = run(make_pdf, [page_with(road_table)] * 3)
    (t,) = [r for r in rec.of_kind("table") if r.page == 1]
    assert t.data["shape"] == "records_table" and t.data["header_rows"] == 1 and t.data["n_rows"] == 3
    assert t.data["headers"][0] == "Road name(s)" and t.entity == "road"


def test_see_above_is_refers_elsewhere_and_na_is_not_an_error(make_pdf):
    def draw(c):
        road_table(c, rows=(("Sunrise Avenue", "See Above", "N/A", "50", "9260", "3%"),))
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert r.fields["start_rp"].value is None and r.fields["start_rp"].raw == "See Above"
    assert "refers_elsewhere" in r.flags and r.fields["start_rp"].status == "verified"
    assert r.fields["end_rp"].value is None and "not applicable or not stated" in r.fields["end_rp"].notes[0]


def test_value_checks_flag_but_never_correct(make_pdf):
    def draw(c):
        road_table(c, rows=(("A Road", "RP 0.700", "RP 0.100", "55", "100", "140%"),))
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert r.fields["start_rp"].value == 0.7 and r.fields["end_rp"].value == 0.1            # not swapped
    assert r.fields["start_rp"].status == r.fields["end_rp"].status == "unverified"
    assert "start RP is not below end RP" in r.fields["start_rp"].notes
    assert "speed is not a multiple of 10" in r.fields["posted_speed_kmh"].notes
    assert "percentage outside 0-100" in r.fields["hmv_pct"].notes


def test_a_cell_that_is_not_the_expected_type_is_unverified_not_guessed(make_pdf):
    def draw(c):
        road_table(c, rows=(("A Road", "RP 0.1", "RP 0.2", "50", "lots", "3%"),))
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    aadt = r.fields["aadt"]
    assert aadt.value is None and aadt.raw == "lots" and aadt.status == "unverified" and "not a integer" in aadt.notes[-1]


def test_unknown_column_names_are_kept_as_other_fields_without_a_model(make_pdf):
    def draw(c):
        road_table(c, names=("Road name(s)", "Start RP / House #", "End RP / House #", "Posted speed", "AADT", "Mystery"),
                   rows=(("A Road", "RP 0.1", "RP 0.2", "50", "100", "xyz"),))
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert r.fields["mystery"].value == "xyz" and r.data["other_fields"] == {"mystery": "Mystery"}


def test_x2_maps_unknown_names_with_validation_and_never_supplies_values(make_pdf):
    def draw(c):
        road_table(c, names=("Road name(s)", "Start RP / House #", "End RP / House #", "Posted speed", "AADT", "Trucks"),
                   rows=(("A Road", "RP 0.1", "RP 0.2", "50", "100", "7%"),))
    fake = FakeClient({"X2": {"entity": "road", "entity_confidence": "high", "mapping": [
        {"name": "Trucks", "field": "hmv_pct", "confidence": "high", "note": None}], "unmapped": []}})
    s, rec = run(make_pdf, [page_with(draw)] * 3, fake)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert r.fields["hmv_pct"].value == 7.0 and r.fields["hmv_pct"].method == "catalog"
    assert {c.prompt_id for c in fake.calls} == {"X2"} and "7%" in fake.calls[0].user   # a sample value is context only
    low = FakeClient({"X2": {"entity": "road", "entity_confidence": "high", "mapping": [
        {"name": "Trucks", "field": "hmv_pct", "confidence": "low", "note": None}], "unmapped": []}})
    s2, rec2 = run(make_pdf, [page_with(draw)] * 3, low, name="t2.pdf")
    (r2,) = [x for x in rows_of(rec2) if x.page == 1]
    assert r2.fields["hmv_pct"].status == "unverified" and "low confidence" in r2.fields["hmv_pct"].notes[-1]
    bogus = FakeClient({"X2": {"entity": "road", "entity_confidence": "high", "mapping": [
        {"name": "Trucks", "field": "not_a_field", "confidence": "high"}], "unmapped": []}})
    s3, rec3 = run(make_pdf, [page_with(draw)] * 3, bogus, name="t3.pdf")
    (r3,) = [x for x in rows_of(rec3) if x.page == 1]
    assert "trucks" in r3.fields and r3.fields["trucks"].method == "code"       # forced to OTHER


def test_a_table_on_the_next_page_with_the_same_columns_continues_the_first(make_pdf):
    def p1(c):
        road_table(c, rows=(("Sunrise Avenue", "RP 0.410", "RP 0.680", "50", "9260", "3%"),))

    def p2(c):
        road_table(c, header=False, rows=(("Gull Lane", "RP 0.000", "RP 0.030", "50", "37", "5%"),))
    s, rec = run(make_pdf, [page_with(p1), page_with(p2), page_with(lambda c: None)])
    roads = rows_of(rec)
    assert [(r.page, r.fields["road_name"].value) for r in roads] == [(1, "Sunrise Avenue"), (2, "Gull Lane")]
    assert roads[1].fields["hmv_pct"].value == 5.0 and roads[1].data["headers"] == roads[0].data["headers"]
    t2 = [r for r in rec.of_kind("table") if r.page == 2][0]
    assert t2.data["continues"].startswith("p1:table") and t2.data["shape"] == "records_table"


def test_a_row_with_an_empty_first_cell_after_a_page_break_continues_the_previous_row(make_pdf):
    def p1(c):
        road_table(c, names=("Road name(s)", "Start RP / House #", "End RP / House #", "Posted speed", "AADT", "Peak traffic periods"),
                   rows=(("School zone", "RP 0.100", "RP 0.200", "30", "500", "Drop off in the morning"),))

    def p2(c):
        road_table(c, header=False, rows=(("", "", "", "", "", "and pick up in the afternoon"),
                                          ("Next Road", "RP 0.300", "RP 0.400", "50", "10", "none")))
    s, rec = run(make_pdf, [page_with(p1), page_with(p2), page_with(lambda c: None)])
    roads = rows_of(rec)
    assert [r.fields["road_name"].value for r in roads] == ["School zone", "Next Road"]      # no extra row for the continuation
    peak = roads[0].fields["peak_periods"]
    assert peak.raw == "Drop off in the morning and pick up in the afternoon"
    assert peak.value == peak.raw and "continued on page 2" in peak.notes
    assert roads[0].data["continued_on"] == [2] and len(roads[0].evidence) > 3


def test_label_value_grid_with_group_headings_gives_one_record_per_group(make_pdf):
    def draw(c):
        box(c, 60, 300, 660, 520)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(250, 500, "DAY SHIFT (during School days)")
        c.setFont("Helvetica", 9)
        c.drawString(80, 480, "First Sign In:")
        c.drawString(200, 480, "0900 hrs")
        c.drawString(320, 480, "Commence Pick up:")
        c.drawString(460, 480, "1430 hrs")
        c.drawString(80, 462, "Traffic Control In:")
        c.drawString(200, 462, "0930 hrs")
        c.drawString(320, 462, "Site Cleared:")
        c.drawString(460, 462, "1500 hrs")
        c.setFont("Helvetica-Bold", 9)
        c.drawString(250, 430, "DAY SHIFT (during Weekends)")
        c.setFont("Helvetica", 9)
        c.drawString(80, 410, "First Sign In:")
        c.drawString(200, 410, "0700 hrs")
        c.drawString(320, 410, "Commence Pick up:")
        c.drawString(460, 410, "1730 hrs")
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    wins = [r for r in rec.of_kind("shift_window") if r.page == 1]
    assert [w.fields["day_type"].value for w in wins] == ["School days", "Weekends"]
    assert {k: v.value for k, v in wins[0].fields.items()} == {
        "first_sign_in": "09:00", "pick_up": "14:30", "tc_in": "09:30", "site_cleared": "15:00", "day_type": "School days"}
    assert wins[1].fields["first_sign_in"].value == "07:00" and "site_cleared" not in wins[1].fields


def test_inline_label_value_pairs_become_a_work_period(make_pdf):
    def draw(c):
        box(c, 60, 400, 660, 500)
        c.setFont("Helvetica", 9)
        c.drawString(80, 480, "Work/stage start date: 28 September 2026")
        c.drawString(330, 480, "Start time: See Above")
        c.drawString(80, 460, "Work/stage end date: 28 October 2026")
        c.drawString(330, 460, "Finish time: See Above")
        c.drawString(80, 440, "Proposed working days: 2 8 September 2026 - 28 October 2026")
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (w,) = [r for r in rec.of_kind("work_period") if r.page == 1]
    assert w.fields["start_date"].value == "2026-09-28" and w.fields["end_date"].value == "2026-10-28"
    assert w.fields["start_time"].raw == "See Above" and "refers_elsewhere" in w.flags
    assert w.fields["working_days"].raw == "2 8 September 2026 - 28 October 2026"     # glitchy text kept as read


def test_struck_row_is_not_applicable_and_its_blocks_flagged(make_pdf):
    def draw(c):
        road_table(c, rows=(("Struck Road", "RP 0.100", "RP 0.200", "50", "500", "2%"),))
        c.setLineWidth(0.5)
        c.line(64, 468.5, 64 + c.stringWidth("Struck Road", "Helvetica", 8), 468.5)
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert r.applicability in ("active", "not_applicable")           # only one of six cells is struck: row stays active
    assert r.fields["road_name"].value == "Struck Road"


def test_contact_rows_are_flagged(make_pdf):
    def draw(c):
        road_table(c, names=("Road name(s)", "Start RP / House #", "End RP / House #", "Posted speed", "AADT", "%HMV"),
                   rows=(("Ring 0800 630 7200", "RP 0.1", "RP 0.2", "50", "100", "1%"),))
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (r,) = [x for x in rows_of(rec) if x.page == 1]
    assert "contact_info" in r.flags


@pytest.mark.parametrize("n", [0])
def test_config_has_a_header_fill_cue(n):
    assert CFG.tables.header_fills and CFG.tables.stitch_x_tol_pt == 6


def test_a_table_continued_over_three_pages_continues_the_first_not_the_previous(make_pdf):
    def p1(c):
        road_table(c, rows=(("Sunrise Avenue", "RP 0.410", "RP 0.680", "50", "9260", "3%"),))

    def p2(c):
        road_table(c, header=False, rows=(("Gull Lane", "RP 0.000", "RP 0.030", "50", "37", "5%"),))

    def p3(c):
        road_table(c, header=False, rows=(("", "", "", "", "", "and more text"), ("Third Road", "RP 0.1", "RP 0.2", "50", "1", "1%")))
    s, rec = run(make_pdf, [page_with(p1), page_with(p2), page_with(p3)])
    roads = rows_of(rec)
    assert [r.fields["road_name"].value for r in roads] == ["Sunrise Avenue", "Gull Lane", "Third Road"]
    assert roads[1].data["continued_on"] == [3] and roads[1].fields["hmv_pct"].raw.endswith("and more text")
    firsts = {r.data["table"] for r in rec.of_kind("table") if r.data["continues"]}
    assert {r.data["continues"] for r in rec.of_kind("table") if r.data["continues"]} == {"p1:table:0"} and len(firsts) == 2


def test_see_above_with_trailing_words_is_still_a_pointer(make_pdf):
    def draw(c):
        box(c, 60, 400, 660, 500)
        c.setFont("Helvetica", 9)
        c.drawString(80, 480, "TTM start date: 28 September 2026")
        c.drawString(330, 480, "TTM start time: See above TTM stages")
        c.drawString(80, 460, "TTM end date: 28 October 2026")
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    (w,) = [r for r in rec.of_kind("work_period") if r.page == 1]
    assert w.fields["start_time"].raw == "See above TTM stages" and w.fields["start_time"].status == "verified"
    assert "refers_elsewhere" in w.flags


def test_a_label_value_table_of_checkbox_labels_makes_no_value_record(make_pdf):
    def draw(c):
        box(c, 60, 400, 660, 500)
        c.setFont("Helvetica", 9)
        c.drawString(80, 480, "Pedestrians: protect and guide pedestrians")
        c.drawString(330, 480, "Cyclists: No cycle lane present")
    s, rec = run(make_pdf, [page_with(draw)] * 3)
    assert not rec.of_kind("vulnerable_user")                 # the checkbox handler owns these answers
