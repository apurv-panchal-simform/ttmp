import pytest

from tmp_pipeline.config import ConfigError, load_config


def test_default_config_has_every_section():
    cfg = load_config()
    for sec in ("page", "intake", "text", "quality", "tables", "ticks", "furniture", "zones", "figures"):
        assert hasattr(cfg, sec)
    assert cfg.zones.form_kind["job pack"] == "narrative"
    assert cfg.zones.header_band_regex.search("TMP Reference KTS 26-1581")   # compiled, case-insensitive
    assert cfg.template == "default" and len(cfg.config_hash()) == 16


def test_template_overrides_nested_keys_only(tmp_path):
    t = tmp_path / "acme.toml"
    t.write_text("[page]\nheader_band_pt = 90\n")
    cfg = load_config(t)
    assert cfg.page.header_band_pt == 90
    assert cfg.page.footer_band_pt == 55


def _bad(tmp_path, body):
    t = tmp_path / "bad.toml"
    t.write_text(body)
    with pytest.raises(ConfigError) as e:
        load_config(t)
    return str(e.value)


def test_typo_in_a_template_key_is_rejected_with_its_path(tmp_path):
    assert "page: unknown key(s) header_band_pts" in _bad(tmp_path, "[page]\nheader_band_pts = 90\n")


def test_wrong_type_names_the_key(tmp_path):
    assert "config.page.header_band_pt: expected a number" in _bad(tmp_path, '[page]\nheader_band_pt = "wide"\n')


def test_invalid_regex_and_out_of_range_values_are_rejected(tmp_path):
    assert "invalid regex" in _bad(tmp_path, '[zones]\nsheet_no_regex = "SHEET("\n')
    assert "quality.low_threshold" in _bad(tmp_path, "[quality]\nlow_threshold = 1.5\n")
    assert "unknown zone" in _bad(tmp_path, '[zones.form_kind]\n"x" = "nowhere"\n')


def test_missing_template_file_and_bad_toml_give_clear_errors(tmp_path):
    with pytest.raises(ConfigError, match="template file not found"):
        load_config(tmp_path / "nope.toml")
    assert "invalid TOML" in _bad(tmp_path, "[page\n")


def test_config_hash_changes_with_the_template(tmp_path):
    t = tmp_path / "acme.toml"
    t.write_text("[page]\nheader_band_pt = 90\n")
    assert load_config(t).config_hash() != load_config().config_hash()
    assert load_config(t).template == "acme"


def test_diagram_section_is_validated_with_its_key_path(tmp_path):
    for body, needle in (("[diagrams]\ntouch_overlap = 2.0\n", "diagrams.touch_overlap"),
                         ("[diagrams]\nnear_dup_bits = 99\n", "diagrams.near_dup_bits"),
                         ("[diagrams]\nenabled = 1\n", "diagrams.enabled"),
                         ("[diagrams.patterns]\nrp_label = '('\n", "diagrams.patterns.rp_label")):
        f = tmp_path / "t.toml"
        f.write_text(body)
        with pytest.raises(ConfigError, match=needle):
            load_config(f)
