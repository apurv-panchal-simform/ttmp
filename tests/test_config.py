from tmp_pipeline.config import load_config


def test_default_config_has_every_section():
    cfg = load_config()
    for sec in ("page", "text", "quality", "tables", "ticks", "furniture", "zones", "figures"):
        assert sec in cfg
    assert cfg["zones"]["form_kind"]["job pack"] == "narrative"


def test_template_overrides_nested_keys_only(tmp_path):
    t = tmp_path / "acme.toml"
    t.write_text("[page]\nheader_band_pt = 90\n")
    cfg = load_config(t)
    assert cfg["page"]["header_band_pt"] == 90
    assert cfg["page"]["footer_band_pt"] == 55
