from tmp_pipeline.versions import VERSIONS, version_stamp


def test_stamp_has_every_component():
    stamp = version_stamp()
    for key in ("extractor", "router", "catalog", "prompt", "model", "chunker", "facts"):
        assert key in stamp and stamp[key]
    assert stamp == VERSIONS
