import pytest

from tmp_pipeline.catalog import CatalogError, load_catalog, normalise


def test_normalise_collapses_case_and_punctuation():
    assert normalise("Road name(s)") == normalise("ROAD  NAME (S)") == "road name s"
    assert normalise("%HMV") == "%hmv" and normalise("School &/or childcare") == "school & or childcare"


def test_default_catalog_loads_with_the_phase_2_entities_and_version():
    c = load_catalog()
    assert c.version and {"road", "risk", "shift_window", "work_period", "vulnerable_user"} <= set(c.entities)
    assert c.entities["road"].match_key == ("road_name", "suburb")


def test_field_lookup_by_synonym_is_normalised():
    c = load_catalog()
    assert c.field_for("road", "Road name(s)").name == "road_name"
    assert c.field_for("road", "Start RP / House #").name == "start_rp"
    assert c.field_for("road", "Road Classification (ONRC)").name == "onrc_class"
    assert c.field_for("road", "nonsense") is None
    assert c.field_for("shift_window", "Commence Pick up:").name == "pick_up"
    assert c.field_for("road", "start_rp").critical and not c.field_for("road", "aadt").critical


def test_match_entity_picks_the_entity_that_explains_most_names():
    c = load_catalog()
    m = c.match_entity(["Road name(s)", "Sub-region / Suburb", "AADT", "Something new"])
    assert m.entity == "road" and m.unmapped == ["Something new"] and m.score == 0.75
    assert c.match_entity(["Initial likelihood", "Residual risk", "Hazard / trigger"]).entity == "risk"
    assert c.match_entity(["totally", "unknown"]) is None


def test_section_types_from_headings_and_their_fields():
    c = load_catalog()
    assert c.section_type("Spills") == "contingency.spills"
    assert c.section_type("Public or Event-related traffic surges") == "contingency.traffic_surges"
    assert c.section_type("Accident:") == "contingency.accident"
    assert c.section_type("Something unknown") is None
    assert set(c.section_fields("contingency.spills")) == {"spill_hotline", "first_action"}
    assert c.is_critical("contingency.traffic_surges", "delay_trigger_minutes")
    assert not c.is_critical("contingency.spills", "first_action")
    assert c.is_critical("risk", "residual_risk")


def _write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_malformed_catalogs_fail_with_a_clear_message(tmp_path):
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "missing.yaml")
    with pytest.raises(CatalogError, match="version"):
        load_catalog(_write(tmp_path, "entities: {}\n"))
    with pytest.raises(CatalogError, match="type must be"):
        load_catalog(_write(tmp_path, 'version: "1"\nentities: {a: {fields: {f: {type: banana}}}}\n'))
    with pytest.raises(CatalogError, match="unknown key"):
        load_catalog(_write(tmp_path, 'version: "1"\nentities: {a: {fields: {f: {type: text, synonym: [x]}}}}\n'))


def test_a_synonym_that_names_two_fields_is_rejected(tmp_path):
    body = 'version: "1"\nentities:\n  a:\n    fields:\n      x: {type: text, synonyms: ["same"]}\n      y: {type: text, synonyms: ["Same"]}\n'
    with pytest.raises(CatalogError, match="already names"):
        load_catalog(_write(tmp_path, body))
