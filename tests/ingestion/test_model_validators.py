from tmp_pipeline.ingestion import prompts
from tmp_pipeline.ingestion.catalog import load_catalog
from tmp_pipeline.ingestion.validators import (
    check_fact,
    check_quote,
    check_x2,
    check_x4,
    check_x6,
    unit_matches,
    value_in_quote,
)

CAT = load_catalog()
TEXT = ("The STMS will monitor the traffic flow at all times. If delays occur exceeding 5mins or 250m in "
        "length, the STMS will remove any visual impacts.")


def test_quote_must_be_verbatim_but_whitespace_is_ignored():
    assert check_quote(TEXT, "If delays occur exceeding 5mins or 250m in length")
    assert check_quote("a  b\nc", "a b c")
    assert not check_quote(TEXT, "If delays occur exceeding 6mins")
    assert not check_quote(TEXT, "")


def test_value_must_sit_inside_its_quote():
    assert value_in_quote(5, "delays exceeding 5mins")
    assert value_in_quote(250, "or 250m in length")
    assert not value_in_quote(6, "delays exceeding 5mins")
    assert value_in_quote("0800 USPILL", "STMS to call 0800 USPILL")
    assert not value_in_quote(True, "ticked") and not value_in_quote(None, "x")
    assert value_in_quote(2.5, "every 2.5 hours")
    assert not value_in_quote(25, "or 250m in length") and not value_in_quote(2, "every 2.5 hours")
    assert not value_in_quote(5, "RP 0.5 to RP 15")
    assert value_in_quote(1158, "An AADT of 1,158 typically")


def test_unit_next_to_the_number_must_match_the_fields_unit():
    minutes = CAT.section_fields("contingency.traffic_surges")["delay_trigger_minutes"]
    assert unit_matches(5, None, "delays exceeding 5mins", minutes)
    assert unit_matches(5, "min", "delays exceeding 5mins", minutes)
    assert not unit_matches(5, "h", "delays exceeding 5mins", minutes)
    assert not unit_matches(5, None, "delays exceeding 5 hours", minutes)      # the quote says hours, field is minutes
    assert unit_matches("text", None, "anything", minutes)


def test_check_fact_collects_every_problem():
    fd = CAT.section_fields("contingency.traffic_surges")["delay_trigger_queue_m"]
    ok = check_fact(TEXT, {"field": "delay_trigger_queue_m", "value": 250, "unit": "m", "quote": "or 250m in length"}, fd)
    assert ok.ok and ok.problems == []
    bad = check_fact(TEXT, {"field": "delay_trigger_queue_m", "value": 300, "unit": "m", "quote": "or 300m in length"}, fd)
    assert not bad.ok and bad.problems == ["quote not found word for word in the section text"]   # invented quote
    wrong_value = check_fact(TEXT, {"value": 25, "unit": "m", "quote": "or 250m in length"}, fd)
    assert wrong_value.problems == ["value does not appear in its quote"]


def test_x6_only_a_literal_true_supports():
    assert check_x6({"supported": True, "reason": "states it"}) == (True, "states it")
    assert check_x6({"supported": "true"})[0] is False
    assert check_x6({"supported": False, "reason": "different meaning"}) == (False, "different meaning")
    assert check_x6(None)[0] is False


def test_x4_type_must_exist_in_the_catalog():
    assert check_x4({"section_type": "contingency.spills", "confidence": "high"}, CAT) == ("contingency.spills", None, "high")
    st, name, conf = check_x4({"section_type": "made.up", "suggested_name": "weather", "confidence": "high"}, CAT)
    assert (st, name, conf) == (None, "weather", "high")
    assert check_x4("junk", CAT) == (None, None, "low")


def test_x2_unknown_fields_become_other_and_low_confidence_is_carried():
    names = ["Road name(s)", "AADT", "Mystery col"]
    res = {"entity": "road", "entity_confidence": "high", "mapping": [
        {"name": "Road name(s)", "field": "road_name", "confidence": "high"},
        {"name": "AADT", "field": "aadt_total", "confidence": "high"},               # not in the catalog
        {"name": "Mystery col", "field": "suburb", "confidence": "low"},
        {"name": "Invented name", "field": "aadt", "confidence": "high"}],            # not one of the table's names
        "unmapped": [{"name": "AADT", "suggested_field": "aadt_total"}]}
    m = check_x2(res, CAT, names)
    assert m.entity == "road" and m.mapped["Road name(s)"] == ("road_name", True)
    assert m.mapped["Mystery col"] == ("suburb", False)
    assert m.other == {"AADT": "aadt_total"} and "aadt" not in {f for f, _ in m.mapped.values()}
    assert any("aadt_total" in p for p in m.problems) and any("Invented name" in p for p in m.problems)


def test_x2_unknown_entity_maps_everything_to_other_and_a_field_cannot_be_used_twice():
    m = check_x2({"entity": "spaceship", "mapping": []}, CAT, ["a", "b"])
    assert m.entity == "unknown" and set(m.other) == {"a", "b"} and not m.mapped
    dup = check_x2({"entity": "road", "entity_confidence": "high", "mapping": [
        {"name": "A", "field": "aadt", "confidence": "high"}, {"name": "B", "field": "aadt", "confidence": "high"}]},
        CAT, ["A", "B"])
    assert dup.mapped == {"A": ("aadt", True)} and "B" in dup.other
    assert check_x2(None, CAT, ["a"]) is None


def test_prompts_carry_version_model_and_the_plan_wording():
    r = prompts.x6_judge("STMS to call 0800 USPILL", "spill_hotline", "0800 USPILL", None, "Phone number")
    assert r.prompt_id == "X6" and r.prompt_version == prompts.PROMPT_VERSIONS["X6"] and r.model == "gpt-6-sol"
    assert "Does the quote state exactly this claim, with this meaning?" in r.user
    assert "Be strict: if the" in r.system and 'Reply JSON: {"supported": true/false' in r.user
    x4 = prompts.x4_match_heading("catalog", "Spills", "Z" * 500)
    assert x4.user.count("Z") == 400 and x4.model == "gpt-6-luna"
    x5 = prompts.x5_extract_facts("contingency.spills", "- spill_hotline", "text")
    assert "character for character" in x5.user and '"other_facts"' in x5.user
    x2 = prompts.x2_match_names(prompts.catalog_text(CAT.entities), "h", "g", "n", "records_table", ["A"], ["1"])
    assert "road: A road affected by the works" in x2.user and "start_rp: Start route position (km)" in x2.user
    assert x2.key() == prompts.x2_match_names(prompts.catalog_text(CAT.entities), "h", "g", "n", "records_table",
                                              ["A"], ["1"]).key()
