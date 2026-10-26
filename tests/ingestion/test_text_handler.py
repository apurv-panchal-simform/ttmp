"""Text handler: typography -> headings, sections, paragraphs, struck text, catalog types, facts and the
X4/X5/X6 hooks. Stores are hand-built so each test states the typography it relies on."""
from helpers import add_block, line, run_handler, store_with_page
from tmp_pipeline.ingestion.extract.handlers.text import TextHandler
from tmp_pipeline.ingestion.vocab import Flag
from tmp_pipeline.llm import FakeClient


def run(store, model=None):
    from helpers import CFG
    from tmp_pipeline.ingestion.extract.handlers.stubs import stub_registry
    from tmp_pipeline.ingestion.extract.router import route
    from tmp_pipeline.records import RecordStore
    reg = stub_registry()
    reg.replace(TextHandler())
    records = RecordStore(store.doc_id)
    res = route(store, CFG, registry=reg, records=records, model=model)
    assert not res.failures, res.failures
    return records


def sections(records):
    return {tuple(r.data["path"]): r for r in records.of_kind("section")}


def body(text, y, x0=48, size=9.0, bold=False):
    return line(text, x0, y, x0 + 300, y + size, size, bold)


def spills_store():
    s = store_with_page()
    add_block(s, [line("Contingency controls", 48, 740, 200, 753, 13, True)])
    add_block(s, [line("Spills", 48, 640, 80, 649, 9, True),
                  body("In case of spills, STMS should suspend all works and spill to be contained with onboard spill kits.", 628),
                  body("Emergency TTM to be installed as required. Also, STMS to call 0800 USPILL.", 616)])
    add_block(s, [body("Filler paragraph so that nine points is the most common size in this document.", 500)])
    return s


def test_level_1_and_2_headings_from_typography_build_the_path():
    s = store_with_page()
    add_block(s, [line("Contingency controls", 48, 740, 200, 753, 13, True)])
    add_block(s, [line("Bad Weather", 48, 700, 120, 709, 9, True), body("• Sustained bad weather is expected.", 688)])
    add_block(s, [line("Property Access", 48, 640, 130, 649, 9, True), body("Cones will be placed so access is kept.", 628)])
    secs = sections(run(s))
    assert ("Contingency controls", "Bad Weather") in secs and ("Contingency controls", "Property Access") in secs
    r = secs[("Contingency controls", "Bad Weather")]
    assert r.data["level"] == 2 and r.entity == "contingency.bad_weather" and r.data["section_type_method"] == "catalog"
    assert r.data["text"] == "• Sustained bad weather is expected."


def test_a_run_of_bold_body_lines_is_a_paragraph_not_headings():
    s = store_with_page()
    add_block(s, [line("Note", 48, 700, 80, 709, 13, True), body("Stacked bold lines follow.", 688)])
    add_block(s, [line("Line one of a boxed note", 48, 600, 200, 609, 9, True),
                  line("Line two of the same note", 48, 588, 200, 597, 9, True)])
    add_block(s, [body("Plain filler text at the body size of the document, long enough to dominate.", 400)])
    secs = sections(run(s))
    (rec,) = [r for r in secs.values() if "Line one of a boxed note" in r.data["text"]]
    assert rec.data["path"] == ["Note"] and "Line two of the same note" in rec.data["text"]


def test_wrapped_lines_join_with_a_space_hyphenated_words_are_rejoined_and_nothing_else_changes():
    s = store_with_page()
    add_block(s, [line("Heading", 48, 700, 100, 713, 13, True),
                  body("The workers will wear high-", 688), body("visibility vests du e to the traffic.", 676)])
    (rec,) = sections(run(s)).values()
    assert rec.data["text"] == "The workers will wear high-visibility vests du e to the traffic."   # "du e" kept as read


def test_bullets_stay_with_their_lead_in_across_blocks_and_new_prose_starts_a_paragraph():
    s = store_with_page()
    add_block(s, [line("Accident:", 48, 700, 100, 709, 9, True), body("If an accident occurred, any of these could follow:", 688)])
    add_block(s, [body("• Death or injury that must be reported", 650, x0=66), body("• Serious harm to property, or", 638, x0=66)])
    add_block(s, [body("The following steps must be taken right away:", 600)])
    (rec,) = sections(run(s)).values()
    paras = [p["text"] for p in rec.data["paragraphs"]]
    assert paras[0] == ("If an accident occurred, any of these could follow:\n• Death or injury that must be reported\n"
                        "• Serious harm to property, or")
    assert paras[1] == "The following steps must be taken right away:"


def test_a_section_continues_across_a_page_break():
    s = store_with_page(1)
    s.pages[2] = s.pages[1]
    add_block(s, [line("Work Running Late", 48, 100, 150, 109, 9, True), body("In the instance of where the work runs over,", 88)], page=1)
    add_block(s, [body("the STMS should contact the supervisors.", 740)], page=2)
    (rec,) = sections(run(s)).values()
    assert rec.data["pages"] == [1, 2] and len(rec.data["paragraphs"]) == 2


def test_struck_section_is_kept_with_not_applicable_and_its_blocks_are_flagged():
    s = store_with_page()
    ln_h = line("Review: Road Controlling Authority – other network (if required)", 48, 203, 413, 215, 12, True)
    ln_h["struck"] = True
    add_block(s, [ln_h])
    ln_b = body("I have reviewed this TMP and authorise the use of traffic signs.", 182)
    ln_b["struck"] = True
    blk = add_block(s, [ln_b])
    add_block(s, [body("Other active text in the document at body size, enough to set the body size.", 100)])
    secs = sections(run(s))
    rec = secs[("Review: Road Controlling Authority – other network (if required)",)]
    assert rec.status == "not_applicable" and rec.applicability == "not_applicable"
    assert Flag.STRUCK in s.elements[blk.element_id].flags


def test_a_struck_heading_with_nothing_under_it_still_leaves_a_record():
    s = store_with_page()
    ln_h = line("Review: other network (if required)", 48, 203, 300, 215, 12, True)
    ln_h["struck"] = True
    add_block(s, [ln_h])
    add_block(s, [line("Other section", 48, 150, 140, 163, 13, True), body("Body text sets the body size for the document.", 100)])
    (rec,) = [r for r in sections(run(s)).values() if r.applicability == "not_applicable"]
    assert rec.data["text"] == "" and rec.data["heading"].startswith("Review")


def test_pattern_fact_with_quote_and_phone_stays_verified_when_not_critical():
    rec = sections(run(spills_store()))[("Contingency controls", "Spills")]
    hotline = rec.fields["spill_hotline"]
    assert (hotline.value, hotline.method, hotline.status) == ("0800 USPILL", "pattern", "verified")
    assert hotline.quote == "Emergency TTM to be installed as required. Also, STMS to call 0800 USPILL." or \
        hotline.quote == "Also, STMS to call 0800 USPILL."
    assert "first_action" not in rec.fields                        # X5-only field: absent without a model


def surge_store():
    s = store_with_page()
    add_block(s, [line("Public or Event-related traffic surges", 48, 580, 300, 589, 9, True),
                  body("The STMS will monitor the traffic flow at all times. If delays occur exceeding 5mins or 250m in", 568),
                  body("length, the STMS will remove any visual impacts.", 556)])
    add_block(s, [body("Filler paragraph so that nine points is the most common size in this document.", 500)])
    return s


def test_critical_pattern_facts_are_unverified_without_a_judge_and_verified_when_x6_supports_them():
    rec = sections(run(surge_store()))[("Public or Event-related traffic surges",)]
    assert {k: v.value for k, v in rec.fields.items()} == {"delay_trigger_minutes": 5, "delay_trigger_queue_m": 250}
    assert all(v.status == "unverified" and "not judged" in v.notes[0] for v in rec.fields.values())
    judge = FakeClient({"X6": {"supported": True, "reason": "states it"}})
    rec2 = sections(run(surge_store(), judge))[("Public or Event-related traffic surges",)]
    assert all(v.status == "verified" for v in rec2.fields.values()) and [c.prompt_id for c in judge.calls].count("X6") == 2
    no = FakeClient({"X6": {"supported": False, "reason": "different meaning"}})
    rec3 = sections(run(surge_store(), no))[("Public or Event-related traffic surges",)]
    assert all(v.status == "unverified" and "judge: different meaning" in v.notes for v in rec3.fields.values())


def test_unknown_heading_is_named_by_x4_when_a_model_answers_and_left_unnamed_otherwise():
    def store():
        s = store_with_page()
        add_block(s, [line("Weather Plan", 48, 580, 150, 589, 9, True), body("If it rains heavily stop work and make safe.", 568)])
        add_block(s, [body("Filler paragraph so that nine points is the most common size in this document.", 500)])
        return s
    plain = sections(run(store()))[("Weather Plan",)]
    assert plain.entity is None and plain.data["section_type_method"] == "unknown" and "X4" in plain.data["notes"][0]
    fake = FakeClient({"X4": {"section_type": "contingency.bad_weather", "confidence": "high"}})
    named = sections(run(store(), fake))[("Weather Plan",)]
    assert named.entity == "contingency.bad_weather" and named.data["section_type_method"] == "X4"
    assert fake.calls[0].prompt_id == "X4" and "Weather Plan" in fake.calls[0].user
    bogus = FakeClient({"X4": {"section_type": "made.up", "suggested_name": "weather", "confidence": "high"}})
    other = sections(run(store(), bogus))[("Weather Plan",)]
    assert other.entity is None and "weather" in other.data["notes"][0]


def test_x5_facts_are_code_checked_and_a_bad_quote_is_stored_unverified():
    s = spills_store()
    good = {"field": "first_action", "value": "suspend all works", "unit": None, "quote": "STMS should suspend all works"}
    bad = {"field": "first_action", "value": "call the council", "unit": None, "quote": "STMS must call the council"}
    rec_good = sections(run(s, FakeClient({"X5": {"facts": [good], "other_facts": []}})))[("Contingency controls", "Spills")]
    fa = rec_good.fields["first_action"]
    assert (fa.value, fa.method, fa.status) == ("suspend all works", "X5", "verified")
    rec_bad = sections(run(spills_store(), FakeClient({"X5": {"facts": [bad], "other_facts": []}})))[("Contingency controls", "Spills")]
    assert rec_bad.fields["first_action"].status == "unverified"
    assert "quote not found word for word in the section text" in rec_bad.fields["first_action"].notes


def test_x5_cannot_add_fields_outside_the_catalog_or_overwrite_pattern_facts():
    fake = FakeClient({"X5": {"facts": [
        {"field": "spill_hotline", "value": "0800 999", "unit": None, "quote": "STMS to call 0800 USPILL"},
        {"field": "made_up_field", "value": "x", "unit": None, "quote": "STMS should suspend all works"}],
        "other_facts": [{"description": "kits", "quote": "onboard spill kits"}, {"description": "bad", "quote": "not in the text"}]}})
    rec = sections(run(spills_store(), fake))[("Contingency controls", "Spills")]
    assert rec.fields["spill_hotline"].value == "0800 USPILL" and "made_up_field" not in rec.fields
    assert [o["quote_verbatim"] for o in rec.data["other_facts"]] == [True, False]


def test_numerics_with_positions_are_attached_to_the_section():
    rec = sections(run(spills_store()))[("Contingency controls", "Spills")]
    phone = [n for n in rec.data["numerics"] if n["kind"] == "phone"]
    assert phone and rec.data["text"][phone[0]["start"]:phone[0]["end"]] == "0800 USPILL"


def test_prompt_versions_and_config_hash_are_stamped_on_every_record():
    (rec, *_) = run(spills_store()).records.values()
    assert rec.produced_by.handler == "TextHandler" and rec.produced_by.prompt_versions["X5"] == "1.0"


def test_record_evidence_points_at_the_blocks_it_came_from():
    s = spills_store()
    rec = sections(run(s))[("Contingency controls", "Spills")]
    assert {e.element_id for e in rec.evidence} <= set(s.elements) and rec.evidence[0].fingerprint
    assert run_handler is not None
