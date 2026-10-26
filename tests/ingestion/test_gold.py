"""The scorer must be able to fail: each measure is exercised with a record store built to break it."""
import json
from pathlib import Path

import pytest

from helpers import add_block, line, store_with_page
from tmp_pipeline.ingestion.score import render, score
from tmp_pipeline.records import Evidence, FieldValue, Record, RecordStore, Stamp

STAMP = Stamp("test", "1")
GOLD_FILE = Path(__file__).parent.parent / "data" / "gold" / "sample.gold.json"


def world():
    s = store_with_page()
    blk = add_block(s, [line("Sunrise Avenue RP 0.410 50", 10, 10, 200, 19)])
    return s, blk


def road(blk, **fields):
    fv = {k: v if isinstance(v, FieldValue) else FieldValue(v, raw=str(v)) for k, v in fields.items()}
    return Record("road", 1, STAMP, fv, "road", [Evidence.of(blk)])


def gold(*expected):
    return {"expected": list(expected)}


def g(fields, **kw):
    return {"kind": "road", "page": 1, "match": {"road_name": "Sunrise Avenue"}, "fields": fields, **kw}


def run(records_list, store, gold_doc):
    rs = RecordStore(store.doc_id)
    for r in records_list:
        rs.add(r)
    return score(rs, store, gold_doc)


def test_a_perfect_run_passes_every_measure():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", start_rp=FieldValue(0.41, raw="RP 0.410"), posted_speed_kmh=50)
    sc = run([r], s, gold(g({"start_rp": 0.41, "posted_speed_kmh": 50, "road_name": "Sunrise Avenue"})))
    assert sc.accuracy == 1.0 and sc.coverage == 1.0 and not sc.invented and sc.passes()
    assert "100.0%" in render(sc)


def test_a_wrong_value_marked_verified_is_counted_and_a_critical_one_separately():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", start_rp=FieldValue(0.5, raw="RP 0.410"))      # critical field (start_rp)
    sc = run([r], s, gold(g({"start_rp": 0.41})))
    assert sc.accuracy == 0.0 and len(sc.wrong_verified) == 1 and len(sc.wrong_verified_critical) == 1
    assert sc.wrong_flagged_rate == 0.0 and not sc.passes()
    assert "expected 0.41, got 0.5" in render(sc)


def test_a_wrong_value_stored_unverified_is_the_right_failure_mode():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", start_rp=FieldValue(0.5, raw="RP 0.410", status="unverified"))
    sc = run([r], s, gold(g({"start_rp": 0.41})))
    assert sc.wrong_verified == [] and sc.wrong_flagged_rate == 1.0 and sc.accuracy == 0.0


def test_missing_field_missing_record_and_coverage():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue")
    other = {"kind": "road", "page": 1, "match": {"road_name": "Gull Lane"}, "fields": {}}
    sc = run([r], s, gold(g({"aadt": 9260}), other))
    assert [x.outcome for x in sc.results] == ["missing"] and sc.missing_records == ["road:p1:1"] and sc.coverage == 0.5


def test_expected_unverified_and_expected_flags_are_checked():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", start_rp=FieldValue(0.41, raw="RP 0.410"))
    sc = run([r], s, gold(g({}, unverified=["start_rp"], flags=["merged"])))
    assert sc.unverified_hit_rate == 0.0 and sc.flag_hit_rate == 0.0 and not sc.passes()
    r2 = road(blk, road_name="Sunrise Avenue", start_rp=FieldValue(0.41, raw="RP 0.410", status="unverified"))
    r2.flags = ["merged"]
    sc2 = run([r2], s, gold(g({}, unverified=["start_rp"], flags=["merged"])))
    assert sc2.unverified_hit_rate == 1.0 and sc2.flag_hit_rate == 1.0


def test_invented_fields_are_those_whose_text_is_not_in_the_cited_elements():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", aadt=FieldValue(9260, raw="9260"))      # 9260 appears nowhere in the evidence
    sc = run([r], s, gold(g({"road_name": "Sunrise Avenue"})))
    assert [(f, raw) for _, f, raw in sc.invented] == [("aadt", "9260")] and not sc.passes()
    assert "not grounded" in render(sc)


def test_a_value_joined_from_two_cells_is_grounded_by_word_not_by_substring():
    s, blk = world()
    blk2 = add_block(s, [line("and pick up in the afternoon", 10, 30, 200, 39)])
    r = road(blk, road_name="Sunrise Avenue")
    r.fields["peak"] = FieldValue("x", raw="Sunrise Avenue and pick up in the afternoon")
    r.evidence.append(Evidence.of(blk2))
    assert run([r], s, gold(g({}))).invented == []


def test_data_keys_entity_and_applicability_are_matched_and_scored():
    s, blk = world()
    sec = Record("section", 1, STAMP, {}, "contingency.spills", [Evidence.of(blk)], applicability="not_applicable",
                 data={"heading": "Spills"})
    gd = gold({"kind": "section", "page": 1, "match": {"data.heading": "Spills"}, "fields": {},
               "entity": "contingency.spills", "applicability": "not_applicable"})
    sc = run([sec], s, gd)
    assert sc.coverage == 1.0 and [x.field for x in sc.results] == ["entity", "applicability"] and sc.accuracy == 1.0
    bad = gold({"kind": "section", "page": 1, "match": {"data.heading": "Spills"}, "fields": {}, "entity": "other"})
    assert run([sec], s, bad).accuracy == 0.0


def test_typed_comparison_ignores_whitespace_and_float_noise_but_not_booleans_vs_numbers():
    s, blk = world()
    r = road(blk, road_name="Sunrise Avenue", hmv_pct=FieldValue(3.0000000001, raw="3%"), peak=FieldValue(" a   b ", raw="a b"),
             flag=FieldValue(True, raw="ticked"))
    sc = run([r], s, gold(g({"hmv_pct": 3, "peak": "a b", "flag": 1})))
    assert [x.outcome for x in sc.results] == ["correct", "correct", "wrong"]


@pytest.mark.sample
def test_the_sample_run_meets_the_phase_2_gold_targets(sample_pdf, tmp_path):
    from tmp_pipeline.ingestion.models import ElementStore
    from tmp_pipeline.ingestion.run import run as run_pipeline
    res = run_pipeline(sample_pdf, tmp_path, render=False)
    sc = score(RecordStore.load(res.out_dir / "records.json"), ElementStore.load(res.out_dir / "elements.json"),
               json.loads(GOLD_FILE.read_text(encoding="utf-8")), facts=res.facts,
               chunks=res.chunks, lineage=res.lineage)
    assert sc.passes(), render(sc)
    assert sc.n_fields > 250 and sc.n_records > 80
    assert sc.conflicts_expected == 4 and sc.conflict_recall == sc.conflict_precision == 1.0   # Phase 4 exit
    assert len(sc.facts_checks) > 25 and sc.facts_exact_rate == 1.0
    assert sc.lineage_complete is True and len(sc.chunk_checks) > 25 and sc.chunks_exact_rate == 1.0   # Phase 5 exit


# --- sheet facts (Phase 3) ------------------------------------------------------------------------------------------

def _sheet_record(symbols, legend=(("green", "Work Area"),), notes=("1M minimum lateral safety zone",),
                  roads=(("Sunrise Avenue", "L1"),)):
    return Record("diagram_sheet", 23, STAMP, {}, "tmd_sheet", [], data={
        "legend": [{"swatch": s, "meaning": m} for s, m in legend], "notes": list(notes),
        "roads": [{"name": n, "level": lv} for n, lv in roads], "symbols": symbols})


def _sym(kind, value=None, rp=None, text=""):
    return {"kind": kind, "value": value, "rp": rp, "text": text}


def _sheet_gold(symbols, **kw):
    sg = {"page": 23, "legend": [["green", "Work Area"]], "notes": ["1M minimum lateral safety zone"],
          "roads": [["Sunrise Avenue", "L1"]], "symbols": symbols, **kw}
    return {"expected": [], "sheets": [sg]}


def _score_sheet(record, gold_doc):
    s = store_with_page()
    rs = RecordStore(s.doc_id)
    if record is not None:
        rs.add(record)
    return score(rs, s, gold_doc)


def test_matching_sheet_facts_pass_and_render_their_rows():
    rec = _sheet_record([_sym("speed_limit", 50), _sym("rp_label", 0.615), _sym("pictogram")])
    sc = _score_sheet(rec, _sheet_gold([{"kind": "speed_limit", "value": 50}, {"kind": "rp_label", "value": 0.615}]))
    assert sc.sheet_exact_rate == 1.0 and sc.symbol_recall == 1.0 and sc.symbol_precision == 1.0 and sc.passes()
    assert "Symbol recall" in render(sc) and "Sheet facts exact" in render(sc)


def test_a_wrong_legend_notes_or_roads_list_fails_the_sheet_measure():
    gold_doc = _sheet_gold([])
    for kw in ({"legend": (("yellow", "Work Area"),)}, {"notes": ("1M minimum safety zone",)}, {"roads": ()}):
        sc = _score_sheet(_sheet_record([], **kw), gold_doc)
        assert sc.sheet_exact_rate < 1.0 and not sc.passes()
    assert "differs from the gold list" in render(sc)


def test_a_missing_symbol_lowers_recall_and_an_extra_one_of_a_gold_kind_lowers_precision():
    gold_doc = _sheet_gold([{"kind": "speed_limit", "value": 50, "n": 2}])
    only_one = _score_sheet(_sheet_record([_sym("speed_limit", 50)]), gold_doc)
    assert only_one.symbol_recall == 0.5 and only_one.symbols_missing and not only_one.passes()
    extra = _score_sheet(_sheet_record([_sym("speed_limit", 50), _sym("speed_limit", 50), _sym("speed_limit", 30)]), gold_doc)
    assert extra.symbol_recall == 1.0 and extra.symbol_precision == pytest.approx(2 / 3) and extra.symbols_extra
    other_kind = _score_sheet(_sheet_record([_sym("speed_limit", 50), _sym("speed_limit", 50), _sym("pictogram")]), gold_doc)
    assert other_kind.symbol_precision == 1.0                  # kinds the gold does not list are not judged


def test_specific_specs_claim_symbols_before_general_ones():
    rec = _sheet_record([_sym("temporary_speed_limit", 30, 0.615), _sym("temporary_speed_limit", 30)])
    gold_doc = _sheet_gold([{"kind": "temporary_speed_limit", "value": 30, "n": 1},
                            {"kind": "temporary_speed_limit", "value": 30, "rp": 0.615}])
    assert _score_sheet(rec, gold_doc).symbol_recall == 1.0
    wrong_rp = _score_sheet(_sheet_record([_sym("temporary_speed_limit", 30, 0.5)]),
                            _sheet_gold([{"kind": "temporary_speed_limit", "value": 30, "rp": 0.615}]))
    assert wrong_rp.symbol_recall == 0.0


def test_a_missing_sheet_record_fails_every_sheet_measure_and_counts_its_symbols_as_missing():
    sc = _score_sheet(None, _sheet_gold([{"kind": "speed_limit", "value": 50}]))
    assert sc.sheet_exact_rate == 0.0 and sc.symbol_recall == 0.0 and sc.symbols_missing and not sc.passes()


def test_symbol_text_matches_ignoring_spacing_and_case():
    rec = _sheet_record([_sym("end_of_works_sign", text="W O RK SE ND")])
    assert _score_sheet(rec, _sheet_gold([{"kind": "end_of_works_sign", "text": "works end"}])).symbol_recall == 1.0


# --- facts (Phase 4) ------------------------------------------------------------------------------------------------

def _facts():
    from tmp_pipeline.ingestion.facts.model import Approval, Conflict, Facts, Signoff, Source, TmdMention
    f = Facts("sha256:t")
    f.conflicts = [Conflict("C-001", 1, "TMP reference differs", [{"value": "KTS 26-1581", "pages": [1, 2]},
                                                                 {"value": "KTS 26-1580", "pages": [1]}])]
    f.signoffs = [Signoff("ttm_designer", 16, "signed"), Signoff("activity_manager", 16, "unsigned")]
    f.approval = Approval("partially_signed")
    f.mentions = [TmdMention("m3:0", "TMD4: Parking Management", 4, "Parking Management", 3, Source("r3:table:0", 3),
                             mapped_to="TMD-01", id_conflict=True)]
    return f


FACTS_GOLD = {"expected": [], "facts": {
    "conflicts": [{"rule": 1, "values": ["KTS 26-1581", "KTS 26-1580"], "pages": [1]}],
    "approval": "partially_signed", "signoffs": {"ttm_designer": "signed", "activity_manager": "unsigned"},
    "mentions": [{"page": 3, "text": "TMD4: Parking Management", "mapped_to": "TMD-01", "id_conflict": True}]}}


def _score_facts(facts, gold=FACTS_GOLD):
    s = store_with_page()
    return score(RecordStore(s.doc_id), s, gold, facts=facts)


def test_matching_facts_pass_and_render_their_rows():
    sc = _score_facts(_facts())
    assert sc.passes(), render(sc)
    assert "Conflicts found (recall) | 100.0% (1/1)" in render(sc)


def test_a_missed_conflict_lowers_recall_and_an_unexpected_one_lowers_precision():
    from tmp_pipeline.ingestion.facts.model import Conflict
    f = _facts()
    f.conflicts = [Conflict("C-001", 4, "Something else", [{"value": "x", "pages": [9]}])]
    sc = _score_facts(f)
    assert sc.conflict_recall == 0.0 and sc.conflict_precision == 0.0 and not sc.passes()
    assert "conflict not found: rule 1" in render(sc) and "conflict not in gold: C-001 rule 4" in render(sc)


def test_wrong_approval_signoff_or_mention_fails_the_facts_measure():
    for spoil in (lambda f: setattr(f.approval, "status", "signed"),
                  lambda f: setattr(f.signoffs[1], "status", "signed"),
                  lambda f: setattr(f.mentions[0], "mapped_to", "TMD-04"),
                  lambda f: setattr(f.mentions[0], "id_conflict", False)):
        f = _facts()
        spoil(f)
        sc = _score_facts(f)
        assert sc.facts_exact_rate < 1.0 and not sc.passes(), render(sc)


def test_facts_measures_are_skipped_without_run_facts():
    s = store_with_page()
    sc = score(RecordStore(s.doc_id), s, FACTS_GOLD)
    assert sc.conflicts_expected == 0 and not sc.facts_checks and sc.passes()


# --- chunks (Phase 5) -----------------------------------------------------------------------------------------------

def test_chunk_measures_fail_on_a_lineage_gap_a_missing_type_an_oversized_chunk_or_a_missing_line():
    from tmp_pipeline.ingestion.chunking.model import Chunk, ChunkSet
    cs = ChunkSet("sha256:t", chunks=[Chunk("T:doc_card:card", "doc_card", "h", "TMP reference: KTS 1", tokens=50,
                                            metadata={"tmp_ref": "KTS 1"})])
    gold = {"expected": [], "chunks": {"types": {"doc_card": 1}, "max_tokens": 600,
                                       "texts": [{"id": "doc_card:card", "contains": ["TMP reference: KTS 1"]}],
                                       "metadata": {"tmp_ref": "KTS 1"}}}
    s = store_with_page()
    ok = score(RecordStore(s.doc_id), s, gold, chunks=cs, lineage={"complete": True, "gaps": []})
    assert ok.passes() and ok.chunks_exact_rate == 1.0, render(ok)
    gap = score(RecordStore(s.doc_id), s, gold, chunks=cs,
                lineage={"complete": False, "gaps": [{"kind": "field", "ref": "road:0.aadt"}]})
    assert not gap.passes() and "lineage gap: field road:0.aadt" in render(gap)
    for spoil in ({"types": {"road": 1}}, {"max_tokens": 10}, {"texts": [{"id": "doc_card", "contains": ["nope"]}]},
                  {"metadata": {"tmp_ref": "KTS 2"}}):
        bad = score(RecordStore(s.doc_id), s, {"expected": [], "chunks": spoil}, chunks=cs, lineage=None)
        assert bad.chunks_exact_rate < 1.0 and not bad.passes(), spoil

