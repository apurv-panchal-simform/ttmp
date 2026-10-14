"""Image facts for image questions: code facts from a sheet's vectors (diagrams.describe), the X13 read check, and the
chunks they become (sheet_text, sheet_layout, sheet_distances, sheet_details, sheet_image)."""
from __future__ import annotations

from helpers import rec
from test_chunks import _chunks
from tmp_pipeline.config import load_config
from tmp_pipeline.diagrams.describe import describe, locate
from tmp_pipeline.diagrams.models import ImageInfo, Region, SheetModel, Symbol
from tmp_pipeline.model.validators import check_x13, garbled_match
from tmp_pipeline.vocab import SymbolKind as K

CFG = load_config().diagrams.describe
FRAME = [0.0, 0.0, 300.0, 300.0]


def _sheet() -> SheetModel:
    """A road along the middle: a sign, a 20 m taper to the work area, a 40 m work area, an inset with house #33."""
    return SheetModel(
        page=21, vector=True, sheet="02", title="SHOULDER CLOSURE",
        title_block={"drawn_by": "A Designer", "scale": "N.T.S", "originator": "Kgwg Trafc SoluƟons Lt."},
        regions=[Region("inset", [220, 200, 300, 300]), Region("notes", [0, 0, 60, 40])],
        images=[ImageInfo(0, FRAME, (300, 300), None, "aerial")],
        notes=["All cones in taper at 2.5M spacings"],
        symbols=[
            Symbol("s1", K.WARNING_SIGN, [20, 140, 40, 160], text="SHOULDER CLOSED", road="Sunrise Avenue", order=1,
                   approach="Sunrise Avenue (RP rising)", rp=0.625),
            Symbol("a1", K.AREA, [100, 140, 180, 150], meaning="Work Area", value="work_area"),
            Symbol("c1", K.CONES, [60, 135, 99, 139], count=5),
            Symbol("d1", K.DIMENSION_LABEL, [70, 170, 80, 175], text="20 m", value=20, unit="m",
                   extra={"span": [60, 165, 100, 180]}),
            Symbol("d2", K.DIMENSION_LABEL, [130, 170, 140, 175], text="40 m", value=40, unit="m",
                   extra={"span": [100, 165, 180, 180]}),
            Symbol("h1", K.HOUSE_LABEL, [130, 200, 140, 210], text="#55", value="55"),
            Symbol("h2", K.HOUSE_LABEL, [250, 250, 260, 260], text="#33", value="33", region="inset"),
            Symbol("w1", K.PLACE_LABEL, [200, 5, 260, 10], text="www.example.com", value="www.example.com"),
        ])


def test_positions_are_a_three_by_three_grid_with_y_up():
    assert locate([10, 280, 20, 290], FRAME) == "top-left"
    assert locate([140, 140, 160, 160], FRAME) == "centre"
    assert locate([280, 10, 290, 20], FRAME) == "bottom-right"
    assert locate([140, 10, 160, 20], FRAME) == "bottom-centre"


def test_code_facts_order_items_and_tie_each_distance_to_the_items_at_its_ends():
    f = describe(_sheet(), (300.0, 300.0), CFG)
    by = {i["id"]: i for i in f["items"]}
    assert "w1" not in by                                                # a web address is not a place
    assert by["h2"]["where"] == "inset" and by["h2"]["location"].startswith("inside the inset")
    assert f["left_to_right"][:3] == ["s1", "d1", "c1"]          # by centre across the page
    d1, d2 = f["distances"]
    assert (d1["value"], d1["start"], d1["end"]) == (20, "the start of the cone run (5 cones)", "the start of the Work Area (shaded area)")
    assert (d2["value"], d2["start"], d2["end"]) == (40, "the start of the Work Area (shaded area)", "the end of the Work Area (shaded area)")
    assert d1["after"] == "d2" and d2["before"] == "d1"
    garbled = [t for t in f["texts"] if t["garbled"]]
    assert [t["text"] for t in garbled] == ["Company: Kgwg Trafc SoluƟons Lt."]


def test_a_broken_font_reading_must_keep_the_printed_letters():
    printed = "PĞ.Ğstrgan ManahĞmĞnt OpƟon: FootpatŚ DgeĞrtĞ. on tŚĞ CarrgahĞway"
    assert garbled_match(printed, "Pedestrian Management Option: Footpath Diverted on the Carriageway") >= 0.85
    assert garbled_match(printed, "Road closed ahead, use the detour") < 0.5


def test_x13_check_flags_unknown_ids_invented_numbers_and_bad_readings():
    result = {"description": "A 40 m work area beside house 55 and a 70 m detour.",
              "items": [{"item_id": "s1", "looks_like": "orange diamond"}, {"item_id": "zz", "looks_like": "x"},
                        {"item_id": "new", "looks_like": "a parked car", "where": "top-left"}],
              "distances": [{"item_id": "d2", "measures": "length of the work area"}, {"item_id": "s1", "measures": "?"}],
              "relations": [{"a": "s1", "relation": "before", "b": "a1"}],
              "garbled_text": [{"id": "g1", "reads_as": "Kiwi Traffic Solutions Ltd"}, {"id": "g9", "reads_as": "x"}],
              "unreadable": [{"where": "inset", "what": "small text"}], "inferred": ["the sign faces west"]}
    chk = check_x13(result, {"s1", "a1", "d2"}, {"d2"}, {40.0, 55.0}, {"g1": "Kgwg Trafc SoluƟons Lt."}, 0.6)
    assert chk.texts["description"]["problems"] == ["number 70 is not on the sheet"]
    assert [i["ok"] for i in chk.items] == [True, False, True]
    assert [d["ok"] for d in chk.distances] == [True, False] and chk.relations[0]["ok"]
    assert len(chk.garbled) == 1 and chk.garbled[0]["ok"]                  # unknown garbled id dropped
    assert chk.unreadable and chk.inferred == ["the sign faces west"]


def _sheet_record(read=None):
    sheet = _sheet()
    facts = describe(sheet, (300.0, 300.0), CFG)
    return rec("diagram_sheet", 21, {"tmd_id": "TMD-07", "sheet": "07", "title": "SHOULDER CLOSURE", "rev": "00"},
               data={"sheet": "07", "symbols": [], "notes": sheet.notes,
                     "legend": [{"swatch": "green", "meaning": "Work Area"}],
                     "describe": {"facts": facts, "read": read}}, entity="tmd_sheet")


def test_the_code_facts_become_text_layout_distance_and_detail_chunks_without_a_model():
    cs, lineage, *_ = _chunks(_sheet_record())
    by = {c.chunk_type: c for c in cs.chunks if "tmd-07" in c.chunk_id}
    assert {"sheet_text", "sheet_layout", "sheet_distances", "sheet_details", "sheet_image"} <= set(by)
    assert "40 m (centre of the plan): the line runs from the start of the Work Area (shaded area) to the end of the Work Area" \
        in by["sheet_distances"].content
    assert "1) warning sign \"SHOULDER CLOSED\" (middle-left of the plan; on Sunrise Avenue; RP 0.625)" in by["sheet_layout"].content
    assert "house #33 (inside the inset" in by["sheet_layout"].content
    assert '"Company: Kgwg Trafc SoluƟons Lt." (printed in a broken font; not read)' in by["sheet_details"].content
    assert "No model has read this image yet" in by["sheet_image"].content
    assert all(c.parent_id and c.parent_id.endswith(":sheet_overview:tmd-07") for c in by.values() if c.chunk_type != "sheet_overview")
    assert lineage["complete"]


def test_a_model_read_adds_description_appearance_and_readings_marked_by_their_checks():
    read = {"texts": {"description": {"text": "A shoulder closure.", "ok": True}, "purpose": {"text": "Close it.", "ok": False}},
            "items": [{"item_id": "s1", "looks_like": "orange diamond", "road_side": "north", "ok": True}],
            "distances": [{"item_id": "d1", "measures": "the taper", "ok": True}],
            "relations": [], "garbled": [{"id": "g1", "printed": "Company: Kgwg Trafc SoluƟons Lt.",
                                          "reads_as": "Company: Kiwi Traffic Solutions Ltd", "ok": True}],
            "unreadable": [{"where": "inset", "what": "small print"}], "inferred": []}
    cs, *_ = _chunks(_sheet_record(read))
    by = {c.chunk_type: c for c in cs.chunks if "tmd-07" in c.chunk_id}
    assert "Description: A shoulder closure." in by["sheet_image"].content
    assert "Purpose: Close it. (unconfirmed)" in by["sheet_image"].content
    assert "Could not be read: small print (inset)" in by["sheet_image"].content
    assert "looks like orange diamond; side of road: north" in by["sheet_layout"].content
    assert "measures: the taper" in by["sheet_distances"].content
    assert '"Company: Kiwi Traffic Solutions Ltd" (printed in a broken font as' in by["sheet_details"].content


def test_the_scorer_catches_invented_values_and_checks_json_answers():
    from tmp_pipeline.gold.questions import _json_in, score_one
    from tmp_pipeline.retrieval.pipeline import AnswerResult
    q = {"id": "x", "row": "structured", "question": "q", "answer": ["35 m"], "must_not": ["40 m"],
         "flags": ["json"], "json_keys": ["road_name", "distance_measurements"]}
    good = AnswerResult("q", 'Here: {"road_name": "Sunrise Avenue", "distance_measurements": ["35 m"]}', "answer", "Q3")
    bad = AnswerResult("q", "35 m and 40 m. {not json}", "answer", "Q3")
    assert _json_in('x {"a": {"b": 1}} y') == {"a": {"b": 1}}
    g, b = score_one(q, good, []), score_one(q, bad, [])
    assert g.checks["no_invented"] and g.checks["json"] and g.row == "structured"
    assert not b.checks["no_invented"] and not b.checks["json"] and b.missing["no_invented"] == ["40 m"]
