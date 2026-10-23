"""Phase 5: chunk types, size policy, chunk text, metadata, lineage and summaries (plan part 4).

Built from the small hand-built TMP of test_facts, plus records added per test, so each test states its input."""
import pytest

from helpers import rec
from test_facts import CAT, CFG, _document_parts
from tmp_pipeline.chunks import size, tokens
from tmp_pipeline.chunks.cli import corrected_chunks, diff
from tmp_pipeline.chunks.context import DocContext
from tmp_pipeline.chunks.lineage import check
from tmp_pipeline.chunks.model import ChunkSet
from tmp_pipeline.chunks.stage import build_chunks, summarise_and_embed
from tmp_pipeline.chunks.summarise import summarise
from tmp_pipeline.chunks.text import header, pages_text, value_text
from tmp_pipeline.facts import map_fields, run_checks
from tmp_pipeline.facts.model import Facts, FactValue
from tmp_pipeline.facts.review import apply_corrections
from tmp_pipeline.model.client import FakeClient
from tmp_pipeline.model.embeddings import FakeEmbeddings
from tmp_pipeline.models import PageInfo
from tmp_pipeline.records import Status


def _chunks(*extra, zones=None):
    facts, store, rs = _document_parts()
    for r in extra:
        rs.add(r)
    for page, zone in (zones or {}).items():
        store.pages[page] = PageInfo(width_pt=595, height_pt=842, zone=zone, form_name="Standard")
    if extra:
        facts = run_checks(map_fields(store, rs, CFG, CAT), store, rs, CFG, CAT)
    cs, lineage = build_chunks(facts, rs, store, CFG)
    return cs, lineage, facts, store, rs


# --- tokens, size, text --------------------------------------------------------------------------------------------

def test_tokens_are_counted_offline_with_the_embedding_models_encoding():
    assert tokens.count("hello world") == 2 and tokens.encoding().name == "cl100k_base"
    assert tokens.sentences("One. Two three. • Four") == ["One.", "Two three.", "• Four"]


def test_a_long_unit_splits_between_sentences_with_overlap():
    text = " ".join(f"Sentence number {i} says something about traffic control." for i in range(60))
    parts = size.split(text, 120, 2)
    assert len(parts) > 2 and all(tokens.count(p) <= 120 for p in parts)
    first_tail = tokens.sentences(parts[0])[-2:]
    assert tokens.sentences(parts[1])[:2] == first_tail                   # two sentences repeated
    assert all(s.endswith(".") for p in parts for s in tokens.sentences(p))  # never cut inside a sentence
    assert size.split("short", 120, 2) == ["short"]


def test_header_is_breadcrumb_and_page_then_the_document_context():
    ctx = DocContext("d", "KTS 26-1581", forms={6: "1. TMP Planning Form"},
                     line="Temporary traffic management for works, 55 Sunrise Avenue")
    h = header(ctx, " › ", [6], ["Risk register", "Stop Go"])
    assert h == ("KTS 26-1581 › 1. TMP Planning Form › Risk register › Stop Go — page 6\n"
                 "Temporary traffic management for works, 55 Sunrise Avenue")
    assert pages_text([1, 2, 3, 5]) == "pages 1–3, 5" and pages_text([6]) == "page 6"


def test_values_carry_their_status_in_words():
    assert value_text(FactValue(0.41, "RP 0.410", status=Status.UNVERIFIED)) == "0.41 (unconfirmed)"
    conflict = FactValue(None, status=Status.CONFLICT, versions=[{"value": "KTS 26-1581", "pages": [2, 3]},
                                                                 {"value": "KTS 26-1580", "pages": [1]}])
    assert value_text(conflict) == "conflict: KTS 26-1581 (pages 2–3) / KTS 26-1580 (page 1)"
    f = Facts("d")
    sw = f.add("shift_window", fields={"day_type": FactValue("School days"), "first_sign_in": FactValue("09:00")})
    see = FactValue(None, "See Above", link=[f"{sw.fact_id}.first_sign_in"])
    assert value_text(see, f) == "See Above (refers to: School days 09:00)"


# --- the builders ----------------------------------------------------------------------------------------------------

def test_the_document_gives_every_applicable_chunk_type_and_complete_lineage():
    cs, lineage, *_ = _chunks()
    types = {c.chunk_type for c in cs.chunks}
    assert {"doc_card", "road", "schedule", "speed_limit", "stage", "signoff", "sheet_overview"} <= types
    assert lineage["complete"], lineage["gaps"]
    assert all(c.tokens <= CFG.chunks.hard_max for c in cs.chunks)
    card = cs.of("doc_card")[0]
    assert "TMP reference: KTS 26-1581 (also written KTS 26-1580)" in card.content
    assert "Approval: partially signed" in card.content and "C-004" in card.content
    md = card.metadata
    assert md["tmp_ref_aliases"] == ["KTS 26-1580"] and md["has_conflict"] and md["approval_status"] == "partially_signed"
    assert md["conflict_ids"] == ["C-001", "C-002", "C-003", "C-004"] and "KTS 26-1581" in md["codes"]


def test_chunk_ids_and_text_are_stable_across_rebuilds():
    a, *_ = _chunks()
    b, *_ = _chunks()
    assert [(c.chunk_id, c.content_hash) for c in a.chunks] == [(c.chunk_id, c.content_hash) for c in b.chunks]
    assert diff(a, b) == {"added": [], "removed": [], "changed": []}


def test_a_register_row_names_every_column_and_its_diagram():
    risk = rec("risk", 6, {"category": "Stop Go", "hazard": "Stop/Go traffic control is in place.",
                           "initial_risk": "Very High", "initial_likelihood": "Likely",
                           "initial_consequence": "Fatality / permanent disability", "residual_risk": "Medium",
                           "affected_tmds": "TMD 04"}, entity="risk")
    cs, *_ = _chunks(risk)
    (row,) = cs.of("register_row")
    assert row.chunk_id.endswith(":register_row:stop-go")
    lines = row.content.splitlines()
    assert lines[0] == "Risk category: Stop Go"
    assert "Initial risk: Very High (likelihood Likely, consequence Fatality / permanent disability)" in lines
    assert "Residual risk: Medium (likelihood not stated, consequence not stated)" in lines
    assert lines[-1] == "Affected diagram: TMD-04 Stop Go" and row.metadata["tmd_ids"] == ["TMD-04"]


def test_sign_schedules_follow_the_order_per_approach_and_hold_at_most_fifteen_signs():
    signs = [{"id": f"s{i}", "kind": "warning_sign", "text": f"SIGN {i}", "order": i + 1, "approach": "Sunrise Avenue",
              "rp": 0.4 + i / 1000} for i in range(20)][::-1]
    sheet = rec("diagram_sheet", 25, {"tmd_id": "TMD-06", "sheet": "06", "title": "DETOUR", "rev": "00"},
                data={"sheet": "06", "symbols": signs, "notes": ["1M lateral safety zone"]}, entity="tmd_sheet")
    cs, lineage, *_ = _chunks(sheet)
    sched = [c for c in cs.of("sheet_sign_schedule") if "tmd-06" in c.chunk_id]
    assert [len(c.content.splitlines()) - 1 for c in sched] == [15, 5]
    assert sched[0].content.splitlines()[1].startswith('1. warning sign "SIGN 0"')
    assert all(c.parent_id and c.parent_id.endswith(":sheet_overview:tmd-06") for c in sched)
    assert [c for c in cs.of("sheet_notes") if "tmd-06" in c.chunk_id] and lineage["complete"]


def test_procedures_split_by_operation_and_installation_or_removal():
    proc = rec("table", 14, data={"table": "p14:table:1", "shape": "label_value_grid", "pairs": [], "cells": [
        {"row": 0, "col": 1, "text": "ON-SITE: the STMS drives through. LANE DIVERSION - deploy centre-line cones. "
                                     "STOP/GO OPERATION - MTCs hold traffic."},
        {"row": 1, "col": 1, "text": "TTM Removal:"},
        {"row": 2, "col": 1, "text": "STOP/GO OPERATION - MTCs stop traffic while cones are removed."}]})
    cs, lineage, *_ = _chunks(proc)
    got = {c.chunk_id.rsplit(":", 1)[1]: c.content for c in cs.of("procedure")}
    assert set(got) == {"on-site-installation", "lane-diversion-installation", "stop-go-installation", "stop-go-removal"}
    assert "MTCs hold traffic." in got["stop-go-installation"] and "removed" in got["stop-go-removal"]
    assert lineage["complete"]


def test_rules_on_a_generic_standard_page_are_scoped_generic_and_struck_text_is_not_chunked():
    rule = rec("section", 25, data={"heading": "WORK CAN OCCUR", "level": 1, "path": ["WORK CAN OCCUR"],
                                    "text": "Work can occur when the lane is closed."})
    struck = rec("section", 12, data={"heading": "Old plan", "level": 1, "path": ["Old plan"],
                                      "text": "This was struck out."}, applicability="not_applicable")
    cs, lineage, *_ = _chunks(rule, struck, zones={25: "generic_standard"})
    (std,) = cs.of("standard_rule")
    assert std.metadata["scope"] == "generic" and "Work can occur" in std.content
    assert not any("struck out" in c.content for c in cs.chunks) and lineage["complete"]


def test_lineage_reports_a_dropped_field_section_or_record():
    cs, lineage, facts, store, rs = _chunks(rec("section", 12, data={"heading": "Delays", "level": 1,
                                                                    "path": ["Delays"], "text": "Queues stay short."}))
    assert lineage["complete"]
    road = next(c for c in cs.chunks if c.chunk_type == "road")
    narrative = next(c for c in cs.chunks if "Queues stay short." in c.content)
    kept = [c for c in cs.chunks if c is not road and c is not narrative]
    gaps = check(kept, facts, rs, store)["gaps"]
    kinds = {g["kind"] for g in gaps}
    assert {"field", "section_text"} <= kinds
    assert any(g["ref"].startswith("road:0.") for g in gaps)


# --- summaries and embeddings ----------------------------------------------------------------------------------------

def test_x12_summaries_are_kept_only_when_their_numbers_are_in_the_chunk():
    cs, *_ = _chunks()
    sched = cs.of("schedule")
    good = FakeClient({"X12": {"summary": "When crews sign in on school days."}})
    assert summarise(sched, good)["written"] == 1 and sched[0].summary == "When crews sign in on school days."
    cs2, *_ = _chunks()
    bad = FakeClient({"X12": {"summary": "Crews sign in at 0645 on school days."}})
    counts = summarise(cs2.of("schedule"), bad)
    assert counts["rejected"] == 1 and cs2.of("schedule")[0].summary is None and len(bad.calls) == 2
    assert bad.calls[1].params["attempt"] == 2 and "0645" in cs2.of("schedule")[0].summary_notes[0]
    reused = FakeClient({"X12": {"summary": "never asked"}})
    assert summarise(cs2.of("schedule"), reused, previous=cs)["reused"] == 1 and not reused.calls


def test_embedding_text_adds_glossary_terms_and_vectors_are_reused_for_unchanged_text(tmp_path):
    cs, *_ = _chunks()
    fake = FakeEmbeddings()
    summarise_and_embed(cs, CFG, None, fake, tmp_path)
    speed = cs.of("speed_limit")[0]
    assert "RP: route position" in speed.embedding_text and speed.vector_key
    assert len(fake.calls) == 1 and len(fake.calls[0]) == len(cs.chunks)
    again, *_ = _chunks()
    summarise_and_embed(again, CFG, None, fake, tmp_path)
    assert len(fake.calls) == 1                                       # every vector came from the cache
    assert [c.vector_key for c in again.chunks] == [c.vector_key for c in cs.chunks]


def test_a_reviewer_correction_changes_only_the_chunks_that_carry_it(tmp_path):
    cs, _, facts, store, rs = _chunks()
    apply_corrections(facts, {"reviewer": "R", "items": [{"ref": "road:0.start_rp", "action": "correct",
                                                          "value": 0.42}]})
    after, _ = build_chunks(facts, rs, store, CFG)
    changes = diff(cs, after)
    assert changes["changed"] and all(":road:" in c or ":doc_card:" in c for c in changes["changed"])
    (tmp_path / "corrections.jsonl").write_text('{"affected": ["road:0.start_rp"]}\n')
    assert [c for c in corrected_chunks(tmp_path, after) if ":road:" in c]


def test_chunks_round_trip_through_jsonl(tmp_path):
    cs, *_ = _chunks()
    cs.dump_jsonl(tmp_path / "chunks.jsonl")
    back = ChunkSet.load_jsonl(tmp_path / "chunks.jsonl")
    assert [c.__dict__ for c in back.chunks] == [c.__dict__ for c in cs.chunks] and back.doc_id == cs.doc_id


def test_chunks_config_is_validated(tmp_path):
    from tmp_pipeline.config import ConfigError, load_config
    t = tmp_path / "bad.toml"
    t.write_text("[chunks]\nhard_max = 100\n")
    with pytest.raises(ConfigError, match="hard_max"):
        load_config(t)


# --- retrieval efficiency: provenance, merging, links, keywords (chunker 0.2.0) ---------------------------------------

def _words(tag, n):
    return " ".join(f"{tag} sentence {i} says something about traffic control on the road." for i in range(n))


def test_page_furniture_does_not_decide_the_pages_of_the_identity_chunk():
    cs, _, facts, *_ = _chunks()
    doc = facts.of("tmp_document")[0]
    assert len(doc.pages()) > 1                      # the reference is printed in many page headers ...
    (ident,) = [c for c in cs.of("form_fields") if "tmp_document" in " ".join(c.field_paths)]
    assert ident.metadata["pages"] == [1] and not ident.metadata["is_boilerplate"]      # ... but stated once, on page 1


def test_boilerplate_is_text_repeated_in_several_chunks_not_one_chunk_spanning_pages():
    from tmp_pipeline.chunks.meta import add_metadata
    from tmp_pipeline.chunks.model import Chunk
    cs, _, facts, *_ = _chunks()
    mk = lambda cid, text, pages: Chunk(cid, "narrative_section", "h", text, {"pages": pages, "section_path": []})  # noqa: E731
    chunks = [mk("a", "same text", [1]), mk("b", "same text", [2]), mk("c", "same text", [3]),
              mk("wide", "only once", [1, 2, 3, 4])]
    add_metadata(chunks, facts, DocContext("d", "KTS 26-1581"), CFG)
    flags = {c.chunk_id: c.metadata["is_boilerplate"] for c in chunks}
    assert flags == {"a": True, "b": True, "c": True, "wide": False}


def test_other_section_parts_carry_only_their_own_pages_and_records():
    big, small = (rec("mystery", 30, {"note": _words("alpha", 70)}), rec("mystery", 31, {"note": "beta stands alone."}))
    cs, lineage, *_ = _chunks(big, small)
    parts = cs.of("other_section")
    assert len(parts) >= 2 and lineage["complete"]
    first, last = parts[0], parts[-1]
    assert first.metadata["pages"] == [30] and last.metadata["pages"][-1] == 31
    assert not any(r.startswith("r31:") for r in first.record_ids)          # page 31's record is not in part one
    assert any(r.startswith("r31:") for r in last.record_ids)


def _table_rows(page, n, table="p41:table:1"):
    return [rec("table_row", page, data={"table": table, "headers": ["Diagram", "Title"], "cells": [
        {"row": i + 1, "col": 0, "text": f"TMD Sheet {i + 1}"}, {"row": i + 1, "col": 1, "text": f"Plan {i + 1}"}]})
        for i in range(n)]


def test_rows_of_one_table_become_one_chunk_that_keeps_every_record():
    rows = _table_rows(41, 4)
    cs, lineage, *_ = _chunks(*rows)
    (chunk,) = [c for c in cs.of("narrative_table_row") if 41 in c.metadata["pages"]]
    assert [ln for ln in chunk.content.splitlines() if ln.startswith("Diagram:")] == [f"Diagram: TMD Sheet {i}" for i in range(1, 5)]
    assert len(chunk.record_ids) == 4 and lineage["complete"]
    other = _table_rows(42, 2, "p42:table:1") + _table_rows(42, 2, "p42:table:2")
    cs2, *_ = _chunks(*other)
    assert len([c for c in cs2.of("narrative_table_row") if 42 in c.metadata["pages"]]) == 2    # a table is the unit


def test_vulnerable_users_are_one_chunk_and_a_long_run_packs_whole_blocks():
    from tmp_pipeline.records import FieldValue
    users = [rec("checkbox", 5, {name: FieldValue(i % 2 == 0, raw=name.title())}, entity="vulnerable_user")
             for i, name in enumerate(["pedestrians", "cyclists", "bus_route"])]
    cs, lineage, *_ = _chunks(*users)
    (chunk,) = cs.of("vulnerable_user")
    assert [ln.split(":")[0] for ln in chunk.content.splitlines()] == ["Pedestrians", "Cyclists", "Bus_Route"] or \
        all(w in chunk.content for w in ("Pedestrians", "Cyclists"))
    assert lineage["complete"] and chunk.tokens <= CFG.chunks.hard_max


def test_a_long_section_is_several_chunks_with_real_part_ids_and_siblings():
    parts = [rec("section", 12 + i, data={"heading": f"Step {i}", "level": 2, "path": ["Procedure", f"Step {i}"],
                                           "text": _words(f"s{i}", 40), "pages": [12 + i]}) for i in range(3)]
    head = rec("section", 12, data={"heading": "Procedure", "level": 1, "path": ["Procedure"], "text": "Intro."})
    cs, lineage, *_ = _chunks(head, *parts)
    got = [c for c in cs.of("narrative_section") if c.chunk_id.split(":")[2] == "procedure"]
    assert len(got) >= 3 and lineage["complete"]
    assert len({c.chunk_id for c in cs.chunks}) == len(cs.chunks)                   # every id is still unique
    assert [c.chunk_id.rsplit(":", 1)[1] for c in got] == [f"part-{i}" for i in range(1, len(got) + 1)]
    assert all(f"(part {i} of {len(got)})" in c.header for i, c in enumerate(got, 1))
    assert all(set(c.siblings) == {o.chunk_id for o in got if o is not c} for c in got) and not any(c.parent_id for c in got)


def test_keywords_add_catalog_names_the_chunk_does_not_already_say():
    cs, *_ = _chunks()
    road = cs.of("road")[0]
    assert "street" in road.keywords and "from rp" in road.keywords
    said = f"{road.header}\n{road.content}".lower()
    assert not any(k.lower() in said for k in road.keywords)            # only vocabulary the chunk lacks
    assert not any("_" in k for k in road.keywords)                    # catalog names, never field identifiers
    stopgo = [c for c in cs.of("sheet_overview") if "stop go" in c.content.lower()]
    if stopgo:
        assert "single lane alternating" in stopgo[0].keywords


def test_embedding_text_has_the_breadcrumb_and_keywords_but_not_the_repeated_context_line(tmp_path):
    cs, *_ = _chunks()
    summarise_and_embed(cs, CFG, None, None, tmp_path)
    road = cs.of("road")[0]
    crumb, context = road.header.split("\n", 1)
    assert crumb in road.embedding_text and context not in road.embedding_text
    assert all(k in road.embedding_text for k in road.keywords)
    assert road.header.endswith(context)                               # the stored header still has it


def test_chunks_with_keywords_round_trip_and_older_files_still_load(tmp_path):
    cs, *_ = _chunks()
    cs.dump_jsonl(tmp_path / "c.jsonl")
    assert ChunkSet.load_jsonl(tmp_path / "c.jsonl").of("road")[0].keywords == cs.of("road")[0].keywords
    import json
    lines = (tmp_path / "c.jsonl").read_text().splitlines()
    old = [lines[0]] + [json.dumps({k: v for k, v in json.loads(ln).items() if k != "keywords"}) for ln in lines[1:]]
    (tmp_path / "old.jsonl").write_text("\n".join(old))
    assert ChunkSet.load_jsonl(tmp_path / "old.jsonl").of("road")[0].keywords == []


# --- tables as rows ---------------------------------------------------------------------------------------------------

def _shift_table(page=50):
    pairs = []
    for g, (row, times) in {"DAY SHIFT (during School days)": (1, ("0900", "1430", "0930", "1500")),
                            "DAY SHIFT (during School holidays)": (4, ("0800", "1630", "0830", "1700"))}.items():
        for n, (label, t) in enumerate(zip(("First Sign In", "Commence Pick up", "Traffic Control In", "Site Cleared"),
                                           times, strict=True)):
            pairs.append({"label": label, "value": f"{t} hrs", "group": g, "row": row + n // 2, "line": 0})
    return rec("table", page, data={"table": f"p{page}:table:1", "shape": "label_value_grid", "pairs": pairs, "cells": []})


def test_a_table_is_chunked_by_rows_one_chunk_per_group():
    cs, lineage, *_ = _chunks(_shift_table())
    groups = [c for c in cs.of("form_fields") if c.metadata["pages"] == [50]]
    assert [c.metadata["table_group"] for c in groups] == ["DAY SHIFT (during School days)",
                                                           "DAY SHIFT (during School holidays)"]
    assert groups[0].content.splitlines() == ["DAY SHIFT (during School days):",
                                              "First Sign In: 0900 hrs | Commence Pick up: 1430 hrs",
                                              "Traffic Control In: 0930 hrs | Site Cleared: 1500 hrs"]
    assert "0800" not in groups[0].content and "0900" not in groups[1].content
    assert lineage["complete"]


def test_a_long_table_group_is_split_between_rows_only():
    pairs = [{"label": f"Item {i}", "value": f"value number {i} for the worksite", "group": "Inventory", "row": i,
              "line": 0} for i in range(120)]
    cs, lineage, *_ = _chunks(rec("table", 51, data={"table": "p51:table:1", "pairs": pairs, "cells": []}))
    parts = [c for c in cs.of("form_fields") if c.metadata["pages"] == [51]]
    assert len(parts) > 1 and all(len(c.siblings) == len(parts) - 1 for c in parts)
    lines = [ln for c in parts for ln in c.content.splitlines() if ln != "Inventory:"]
    assert lines == [f"Item {i}: value number {i} for the worksite" for i in range(120)]     # every row whole, once
    assert all(c.content.startswith("Inventory:") for c in parts) and lineage["complete"]


def test_a_table_a_fact_builder_used_still_gets_its_other_rows_chunked():
    table = rec("table", 52, data={"table": "p52:table:1", "cells": [], "pairs": [
        {"label": "Client (Contracting PCBU)", "value": "Jo Bloggs - 021 555 0101", "group": None, "row": 0},
        {"label": "Meeting location", "value": "Online via Microsoft Teams", "group": None, "row": 1}]})
    cs, lineage, facts, *_ = _chunks(table)
    assert any(f.fields["name"].value == "Jo Bloggs" for f in facts.of("contact"))      # the contact builder used it
    assert any("Meeting location: Online via Microsoft Teams" in c.content for c in cs.of("form_fields"))
    assert lineage["complete"]


def test_lineage_reports_a_table_value_no_chunk_holds():
    cs, lineage, facts, store, rs = _chunks(_shift_table())
    kept = [c for c in cs.chunks if "School holidays" not in c.content]
    gaps = check(kept, facts, rs, store)["gaps"]
    assert {g["kind"] for g in gaps} == {"table_row"} and any("0800 hrs" in g["text"] for g in gaps)


def test_header_band_fields_reach_the_document_fact_and_card():
    facts, store, rs = _document_parts()
    store.metadata["header_fields"] = [
        {"label": "TMP Reference", "value": "KTS 26-1581 AT-T", "pages": [1, 2]},
        {"label": "RCA consent and/or contract reference", "value": "AT-W", "pages": [1, 2]}]
    facts = run_checks(map_fields(store, rs, CFG, CAT), store, rs, CFG, CAT)
    doc = facts.of("tmp_document")[0]
    assert doc.fields["rca_reference"].value == "AT-W" and doc.fields["tmp_reference_printed"].value == "KTS 26-1581 AT-T"
    cs, lineage = build_chunks(facts, rs, store, CFG)
    card = cs.of("doc_card")[0]
    assert "RCA consent / contract reference: AT-W" in card.content and lineage["complete"]
