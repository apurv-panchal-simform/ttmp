"""Phase 4: field mapper, diagram ids, pointers, sign-offs, checks and the review list (plan part 3).

Records are built by hand, so each test states exactly what the handlers gave the mapper."""
import json

import pytest

from helpers import add_block, line, pairs_table, rec, record_store, sheet_record, store_with_page
from tmp_pipeline.config import ConfigError, load_config
from tmp_pipeline.ingestion.catalog import load_catalog
from tmp_pipeline.ingestion.facts import Facts, map_fields, run_checks
from tmp_pipeline.ingestion.facts.mentions import find_mentions, stage_lists
from tmp_pipeline.ingestion.facts.merge import merge_entities
from tmp_pipeline.ingestion.facts.model import Signoff
from tmp_pipeline.ingestion.facts.parse import contacts_from_pairs, order_reference, tsl_rows
from tmp_pipeline.ingestion.facts.pointers import resolve_pointers
from tmp_pipeline.ingestion.facts.review import CorrectionError, apply_corrections, apply_corrections_file
from tmp_pipeline.ingestion.facts.signoff import approval, read_signoffs
from tmp_pipeline.ingestion.facts.tmd import map_mentions, sheet_register
from tmp_pipeline.ingestion.models import PageInfo
from tmp_pipeline.llm import FakeClient
from tmp_pipeline.records import FieldValue, Status

CFG = load_config()
FC = CFG.facts
CAT = load_catalog()
SHEETS = [sheet_record(20, "01", "NO PARKING PLAN"), sheet_record(21, "02", "SHOULDER CLOSURE"),
          sheet_record(22, "03", "LANE DIVERSION"), sheet_record(23, "04", "STOP GO"),
          sheet_record(24, "05", "UNATTENDED WORKSITE")]


def _sheets():
    return [sheet_record(s.page, s.data["sheet"], s.fields["title"].value) for s in SHEETS]


def _mapped(*records, model=None):
    rs = record_store(*_sheets(), *records)
    ms = find_mentions(rs, FC)
    map_mentions(ms, sheet_register(rs), FC, CAT, model)
    return ms


# --- mentions and the one-id-per-diagram rule --------------------------------------------------------------------

def test_mentions_read_every_spelling_and_the_title_after_the_number():
    ms = _mapped(pairs_table(3, [("TMD4", "Parking Management"), ("TMD-02", "Shoulder Closure, dayshift")],
                             group="TTM STAGES:"),
                 rec("risk", 6, {"affected_tmds": "TMD 02 / TMD 03"}, entity="risk"),
                 rec("table_row", 15, data={"cells": [{"col": 0, "text": "TMD Sheet 04"}, {"col": 1, "text": "Stop Go"}]}))
    got = [(m.page, m.number, m.title) for m in ms]
    assert got == [(3, 4, "Parking Management"), (3, 2, "Shoulder Closure"), (6, 2, None), (6, 3, None),
                   (15, 4, "Stop Go")]
    assert {m.page for m in ms} & {20, 21, 22, 23, 24} == set()          # title blocks are the register, not mentions


def test_a_cut_off_number_only_copy_on_the_same_page_is_dropped():
    ms = _mapped(pairs_table(5, [("Pedestrians", "guide pedestrians as per TMD 01 – Shoulder Closure")]),
                 rec("checkbox", 5, {"pedestrians": True}, data={"note": "guide pedestrians as per TMD 01 –"},
                     entity="vulnerable_user"))
    assert [(m.number, m.title) for m in ms] == [(1, "Shoulder Closure")]


def test_title_wins_over_number_number_only_matches_by_number_and_agreement_is_plain():
    ms = _mapped(pairs_table(3, [("TMD4", "Parking Management"), ("TMD3", "Lane Diversion")]),
                 rec("risk", 6, {"affected_tmds": "TMD 05"}, entity="risk"))
    parking, lane, number_only = ms
    assert (parking.mapped_to, parking.method, parking.id_conflict) == ("TMD-01", "title", True)
    assert "the title wins" in parking.notes[-1]
    assert (lane.mapped_to, lane.method, lane.id_conflict) == ("TMD-03", "number_and_title", False)
    assert (number_only.mapped_to, number_only.method) == ("TMD-05", "number")


def test_the_conflicting_mention_becomes_an_alias_of_the_sheet_it_names():
    rs = record_store(*_sheets(), pairs_table(3, [("TMD4", "Parking Management")]))
    sheets = sheet_register(rs)
    map_mentions(find_mentions(rs, FC), sheets, FC, CAT)
    assert "TMD4: Parking Management" in sheets[0].aliases


def test_a_title_that_matches_no_sheet_or_two_sheets_is_never_guessed():
    ms = _mapped(pairs_table(3, [("TMD7", "Footpath Detour")]),
                 rec("table_row", 4, data={"cells": [{"col": 0, "text": "TMD 02 Closure Diversion"}]}))
    assert all(m.mapped_to is None and m.status == Status.UNVERIFIED for m in ms)
    assert "no model answered (X11)" in ms[0].notes
    assert "matches 2 sheets equally" in ms[1].notes[0]


@pytest.mark.parametrize("answer, mapped, status", [
    ({"tmd_id": "TMD-04", "confidence": "high", "reason": "stop/go"}, "TMD-04", Status.VERIFIED),
    ({"tmd_id": "TMD-04", "confidence": "medium", "reason": "probably"}, "TMD-04", Status.UNVERIFIED),
    ({"tmd_id": "TMD-09", "confidence": "high", "reason": "invented"}, None, Status.UNVERIFIED),
    ({"tmd_id": "TMD-04", "confidence": "low", "reason": "unsure"}, None, Status.UNVERIFIED),
    ({"tmd_id": "none", "confidence": "high", "reason": "nothing fits"}, None, Status.UNVERIFIED),
])
def test_x11_answers_are_checked_before_a_mention_is_mapped(answer, mapped, status):
    model = FakeClient({"X11": answer})
    (m,) = _mapped(pairs_table(3, [("TMD 4", "Alternating Shuttle Control")]), model=model)
    assert (m.mapped_to, m.status) == (mapped, status)
    assert model.calls and "TMD-04: STOP GO" in model.calls[0].user
    assert m.id_conflict is False and (m.method == "X11" or mapped is None)


def test_stage_lists_are_the_mentions_under_a_staging_label():
    ms = _mapped(pairs_table(3, [("TMD2", "Shoulder Closure"), ("TMD3", "Lane Diversion")], group="TTM STAGES:"),
                 pairs_table(11, [("TMD 01", "PARKING MANAGEMENT")]))
    assert [[m.mapped_to for m in v] for v in stage_lists(ms, FC).values()] == [["TMD-02", "TMD-03"]]


# --- merging ----------------------------------------------------------------------------------------------------

def _merge(*records):
    f = Facts("sha256:test")
    merge_entities(f, list(records), CAT, FC)
    return f


def test_same_road_merges_after_names_are_normalised_and_clashes_keep_every_version():
    f = _merge(rec("road", 4, {"road_name": "Sunrise Ave", "suburb": "Murrays Bay", "aadt": 9260}, entity="road"),
               rec("road", 18, {"road_name": "Sunrise Avenue", "suburb": "Murrays Bay", "aadt": 9300,
                                "posted_speed_kmh": 50}, entity="road"),
               rec("road", 18, {"road_name": "Gull Lane", "suburb": "Mairangi Bay"}, entity="road"))
    sunrise, gull = f.of("road")
    assert sunrise.pages() == [4, 18] and gull.pages() == [18]
    assert sunrise.fields["posted_speed_kmh"].value == 50 and sunrise.fields["road_name"].method == "merge"
    aadt = sunrise.fields["aadt"]
    assert aadt.status == Status.CONFLICT and aadt.value is None
    assert [(v["value"], v["pages"]) for v in aadt.versions] == [(9260, [4]), (9300, [18])]


def test_a_record_missing_a_key_field_still_merges_when_the_match_is_unique():
    f = _merge(rec("road", 4, {"road_name": "Sunrise Avenue", "suburb": "Murrays Bay"}, entity="road"),
               rec("road", 18, {"road_name": "Sunrise Avenue"}, entity="road"))
    assert len(f.of("road")) == 1


def test_one_per_document_entities_and_day_types_merge():
    f = _merge(rec("work_period", 4, {"start_date": "2026-09-28"}, entity="work_period"),
               rec("work_period", 11, {"start_date": "2026-09-28", "end_date": "2026-10-28"}, entity="work_period"),
               rec("shift_window", 3, {"day_type": "School days", "tc_in": "09:30"}, entity="shift_window"),
               rec("shift_window", 11, {"day_type": "during school days", "tc_in": "09:30"}, entity="shift_window"))
    (wp,) = f.of("work_period")
    (sw,) = f.of("shift_window")
    assert wp.pages() == [4, 11] and wp.fields["start_date"].status == Status.VERIFIED and sw.pages() == [3, 11]


# --- pointers ---------------------------------------------------------------------------------------------------

def _forms(store, forms):
    for page, form in forms.items():
        store.pages[page] = PageInfo(width_pt=595, height_pt=842, form_name=form)
    return store


def test_see_above_links_to_the_nearest_earlier_place_in_the_same_form():
    see = FieldValue(None, raw="See Above", notes=["refers elsewhere (resolved in a later phase)"])
    f = _merge(rec("work_period", 4, {"start_time": see}, entity="work_period", flags=["refers_elsewhere"]),
               rec("shift_window", 3, {"day_type": "School days", "first_sign_in": "09:00"}, entity="shift_window"),
               rec("shift_window", 11, {"day_type": "Weekends", "first_sign_in": "07:00"}, entity="shift_window"))
    store = _forms(store_with_page(), {3: "Planning", 4: "Planning", 11: "Job Pack"})
    assert resolve_pointers(f, store, FC) == []
    fv = f.of("work_period")[0].fields["start_time"]
    assert fv.link == ["shift_window:0.first_sign_in"] and fv.method == "pointer" and fv.value is None


def test_a_pointer_with_nothing_earlier_stays_unverified():
    see = FieldValue(None, raw="See Above", notes=["refers elsewhere (resolved in a later phase)"])
    f = _merge(rec("work_period", 4, {"start_time": see}, entity="work_period"))
    unresolved = resolve_pointers(f, _forms(store_with_page(), {4: "Planning"}), FC)
    assert unresolved == ["work_period:0.start_time"]
    assert f.of("work_period")[0].fields["start_time"].status == Status.UNVERIFIED


# --- text parsers -----------------------------------------------------------------------------------------------

def test_a_temporary_speed_limit_row_is_parsed_into_typed_values():
    row = rec("table_row", 18, data={"headers": ["TSL details", "Times", "Dates"], "cells": [
        {"col": 0, "text": "A temporary speed limit of 30 km/h on Sunrise Avenue for 150m between RP 0.615 and RP 0.465."},
        {"col": 1, "text": "9am – 3pm (during school days) 7am –6pm (during weekends)"},
        {"col": 2, "text": "28 September 2026 To 28 October 2026"}]})
    (tsl,) = tsl_rows(record_store(row), ["Sunrise Avenue"], FC)
    v = {k: fv.value for k, fv in tsl.fields.items()}
    assert (v["speed_kmh"], v["length_m"], v["rp_from"], v["rp_to"], v["road_name"]) == (30, 150, 0.465, 0.615,
                                                                                        "Sunrise Avenue")
    assert [(h["day_type"], h["from"], h["to"]) for h in v["hours_by_day_type"]] == [
        ("school_days", "09:00", "15:00"), ("weekends", "07:00", "18:00")]
    assert (v["date_from"], v["date_to"]) == ("2026-09-28", "2026-10-28")
    not_required = rec("table_row", 18, data={"headers": ["TSL details"], "cells": [{"col": 0, "text": "Not Required"}]})
    assert tsl_rows(record_store(not_required), [], FC) == []


def test_order_reference_and_contacts_come_from_their_pairs():
    t = pairs_table(1, [("TMP Order / Reference", "DOM-354922 / KTS 26-1580"),
                        ("Client (Contracting PCBU)", "James Lu - 021 733 454"), ("Subcontractor PCBU(s)", "N/A")])
    rs = record_store(t)
    assert order_reference(rs, FC).value == "DOM-354922"
    assert [c for c, _ in contacts_from_pairs(rs, FC)] == [
        {"name": "James Lu", "role": "Client (Contracting PCBU)", "phone": "021 733 454"}]


# --- sign-offs and approval -------------------------------------------------------------------------------------

def _signoff_table(store, page, y_top, heading, rows, *, struck=False):
    """A table element under a heading text block; ``rows`` is the cell grid (list of lists of str)."""
    h = add_block(store, [line(heading, 48, y_top + 6, 300, y_top + 18, bold=True)], page=page)
    if struck:
        h.flags = ["struck"]
    tbl = store.add(page, (595, 842), "forms", "table", [42, y_top - 20 * len(rows), 553, y_top], source="test")
    cells = [{"row": r, "col": c, "text": t} for r, row in enumerate(rows) for c, t in enumerate(row)]
    return rec("table", page, data={"table": tbl.element_id, "shape": "unknown", "cells": cells, "pairs": []})


def _signoff_doc(form3=True, checker_signature="S. Singh"):
    store = store_with_page(16)
    tables = [
        _signoff_table(store, 16, 700, "TTM Designer", [["Name", "Email", "Phone"], ["Dimple Manipon", "", ""],
                                                       ["Organisation and role", "Signature", "Date"],
                                                       ["Kiwi Traffic", "D. Manipon", "16/09/2026"],
                                                       ["Checked by:", "Signature", "Date"],
                                                       ["Satvir Singh", checker_signature, "16/09/2026"]]),
        _signoff_table(store, 16, 500, "Activity Manager", [["Name", "Email", "Phone"], ["Sam Parnell", "", ""],
                                                            ["Organisation and role", "Signature", "Date"],
                                                            ["Fulton Hogan Ltd", "", "16/09/2026"]]),
        _signoff_table(store, 16, 340, "Review: Road Controlling Authority – primary network",
                       [["Organisation and role", "Signature", "Date"], ["Auckland Transport", "", ""]]),
        _signoff_table(store, 16, 200, "Review: Road Controlling Authority – other network",
                       [["Organisation and role", "Signature", "Date"], ["", "", ""]], struck=True)]
    if form3:
        store.pages[19] = PageInfo(width_pt=595, height_pt=842)
        tables.append(_signoff_table(store, 19, 330, "Road controlling authority",
                                     [["I authorise this request to: 1. Install temporary speed limit(s)."],
                                      ["Name", "Signature", "Date"]]))
    return store, record_store(*tables)


def test_signoff_blocks_are_read_by_label_and_role():
    store, rs = _signoff_doc()
    got = {s.role: (s.status, s.name, s.signature) for t in read_signoffs(store, rs, FC) for s in t.signoffs}
    assert got == {"ttm_designer": ("signed", "Dimple Manipon", "D. Manipon"),
                   "design_checker": ("signed", "Satvir Singh", "S. Singh"),
                   "activity_manager": ("unsigned", "Sam Parnell", None),
                   "rca_primary": ("unsigned", None, None),
                   "rca_other": ("not_applicable", None, None),
                   "rca_authorisation_form3": ("unsigned", None, None)}


def test_a_date_alone_is_not_a_signature():
    store, rs = _signoff_doc(checker_signature="16/09/2026")
    checker = next(s for t in read_signoffs(store, rs, FC) for s in t.signoffs if s.role == "design_checker")
    assert checker.status == "unsigned" and "only a date" in checker.notes[0]


def test_approval_counts_required_blocks_and_form_3_only_when_a_limit_is_requested():
    def so(role, status):
        return Signoff(role, 16, status)
    signed = [so(r, "signed") for r in ("ttm_designer", "design_checker", "activity_manager", "rca_primary")]
    assert approval(signed, CAT, {"tsl_requested": False}).status == "signed"
    with_form3 = approval([*signed, so("rca_authorisation_form3", "unsigned")], CAT, {"tsl_requested": True})
    assert with_form3.status == "partially_signed" and with_form3.unsigned == ["rca_authorisation_form3"]
    none = approval([so("ttm_designer", "unsigned")], CAT, {})
    assert none.status == "unsigned" and "no sign-off block found for design_checker" in none.notes


# --- the whole mapper and the checks -----------------------------------------------------------------------------

def _document(two_way=True, tsl_hours="9am – 3pm (during school days)", reference="KTS 26-1580"):
    return _document_parts(two_way, tsl_hours, reference)[0]


def _document_parts(two_way=True, tsl_hours="9am – 3pm (during school days)", reference="KTS 26-1580"):
    """(facts, store, records) of a small hand-built TMP with the four kinds of conflict."""
    store, rs = _signoff_doc()
    for p in (1, 3, 4, 12, 18, 20, 21, 22, 23, 24):
        store.pages[p] = PageInfo(width_pt=595, height_pt=842, form_name="Planning", tmp_reference=
                                  "KTS 26-1581" if p in (3, 4) else None)
    for r in [*_sheets(),
              pairs_table(1, [("TMP Order / Reference", f"DOM-1 / {reference}")]),
              pairs_table(3, [("TMD4", "Parking Management"), ("TMD2", "Shoulder Closure")], group="TTM STAGES:"),
              rec("shift_window", 3, {"day_type": "School days", "first_sign_in": "09:00", "site_cleared": "15:00"},
                  entity="shift_window"),
              rec("road", 4, {"road_name": "Sunrise Avenue", "start_rp": 0.41, "end_rp": 0.68}, entity="road"),
              rec("work_period", 4, {"start_date": "2026-09-28", "end_date": "2026-10-28"}, entity="work_period"),
              rec("section", 12, data={"heading": "Delay", "path": ["Delay"], "text":
                                       "Two-way traffic will be maintained throughout." if two_way else "Delays."}),
              rec("table_row", 18, data={"headers": ["TSL details"], "cells": [{"col": 0, "text":
                  f"30 km/h on Sunrise Avenue between RP 0.465 and RP 0.615. {tsl_hours} "
                  "28 September 2026 To 28 October 2026"}]})]:
        rs.add(r)
    facts = map_fields(store, rs, CFG, CAT)
    return run_checks(facts, store, rs, CFG, CAT), store, rs


def test_the_four_document_conflicts_get_ids_in_rule_order():
    f = _document()
    assert [(c.conflict_id, c.rule) for c in f.conflicts] == [("C-001", 1), ("C-002", 2), ("C-003", 3), ("C-004", 4)]
    c1, c2, c3, c4 = f.conflicts
    assert {v["value"] for v in c1.versions} == {"KTS 26-1581", "KTS 26-1580"}
    assert f.of("tmp_document")[0].fields["tmp_reference"].status == Status.CONFLICT
    assert c2.versions[0]["value"] == "TMD4: Parking Management" and c2.versions[0]["mapped_to"] == "TMD-01"
    assert "TMD-03" in c3.versions[0]["value"] and "TMD-04" in c3.versions[0]["value"]
    assert c4.versions[1]["value"] == "TMD-04 STOP GO" and c4.versions[0]["pages"] == [12]
    checks = {c.rule: c.outcome for c in f.checks}
    assert checks[5] == checks[6] == checks[7] == checks[8] == "pass"
    assert f.approval.status == "partially_signed" and f.coverage["records_unaccounted"] == []


def test_a_consistent_document_raises_no_conflict_for_rules_1_and_4():
    f = _document(two_way=False, reference="KTS 26-1581")
    assert {c.rule for c in f.conflicts} == {2, 3}
    assert {c.rule: c.outcome for c in f.checks}[1] == "pass"


def test_speed_limit_hours_that_differ_from_the_shift_window_conflict():
    f = _document(tsl_hours="8am – 3pm (during school days)")
    (c,) = [c for c in f.conflicts if c.rule == 6]
    assert c.versions[0]["value"] == "08:00-15:00 (school_days)" and c.versions[1]["value"] == "09:00-15:00"


def test_review_list_holds_conflicts_and_unsigned_blocks_and_corrections_mark_values_reviewed(tmp_path):
    f = _document()
    kinds = {r.kind for r in f.review}
    assert {"conflict", "unsigned"} <= kinds
    path = "tmp_document:0.tmp_reference"
    touched = apply_corrections(f, {"reviewer": "R", "items": [
        {"ref": "C-001", "action": "correct", "value": "KTS 26-1581", "note": "typo"},
        {"ref": "road:0.start_rp", "action": "confirm"}]})
    assert touched == [path, "C-001", "road:0.start_rp"] and f.revision == 2
    fv = f.value(path)
    assert fv.status == Status.REVIEWED and fv.value == "KTS 26-1581" and fv.review["original"]["status"] == "conflict"
    assert f.value("road:0.start_rp").status == Status.REVIEWED
    assert next(r for r in f.review if r.ref == "C-001").status == "closed"
    with pytest.raises(CorrectionError, match="names no value"):
        apply_corrections(f, {"reviewer": "R", "items": [{"ref": "road:9.x", "action": "confirm"}]})
    with pytest.raises(CorrectionError, match="reviewer"):
        apply_corrections(f, {"items": []})
    f.dump(tmp_path / "facts.json")
    (tmp_path / "c.json").write_text(json.dumps({"reviewer": "R", "items": [{"ref": "road:0.end_rp",
                                                                             "action": "correct", "value": 0.7}]}))
    apply_corrections_file(tmp_path, tmp_path / "c.json")
    again = Facts.load(tmp_path / "facts.json")
    assert again.revision == 3 and again.value("road:0.end_rp").value == 0.7
    assert len((tmp_path / "corrections.jsonl").read_text().splitlines()) == 1
    assert json.loads((tmp_path / "review.json").read_text())["revision"] == 3


def test_facts_round_trip_through_json(tmp_path):
    f = _document()
    f.dump(tmp_path / "facts.json")
    back = Facts.load(tmp_path / "facts.json")
    assert back.to_json() == f.to_json()
    assert isinstance(back.conflicts[0].versions, list) and back.approval.status == f.approval.status


def test_stages_link_to_their_own_diagram_and_the_speed_limit_to_the_sheets_it_names():
    f = _document()
    assert [(s.fields["tmd_id"].value, s.links["tmd"]) for s in f.of("stage")] == [("TMD-01", ["TMD-01"]),
                                                                                  ("TMD-02", ["TMD-02"])]
    assert f.of("speed_limit")[0].fields["speed_kmh"].value == 30


# --- config and catalog -----------------------------------------------------------------------------------------

def test_facts_config_is_validated_with_its_key_path(tmp_path):
    t = tmp_path / "bad.toml"
    t.write_text('[facts]\nincompatible_modes = ["two_way:flying"]\n')
    with pytest.raises(ConfigError, match="incompatible_modes"):
        load_config(t)
    t.write_text('[facts.signoff_labels]\nsignature = "(?i)^sig"\nname = "x"\ndate = "y"\n')
    assert load_config(t).facts.signoff_labels["checked_by"]          # tables merge: other labels kept
    t.write_text("[facts]\ntitle_max_word = 3\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(t)


def test_facts_entities_never_take_part_in_name_matching():
    assert "signoff" in CAT.facts_entities and "signoff" not in CAT.entities
    assert CAT.match_entity(["Name", "Email", "Phone"]) is None
    assert [r.role for r in CAT.required_signoffs if r.condition == "tsl_requested"] == ["rca_authorisation_form3"]
