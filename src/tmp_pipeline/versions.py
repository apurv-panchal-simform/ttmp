"""Version stamps recorded on every run, value and chunk (plan section 1.1)."""

VERSIONS = {
    "extractor": "0.1.0",
    "router": "1.0.0",
    "catalog": "0.0.0",
    "prompt": "0.0.0",
    "model": "none",
    "chunker": "0.0.0",
}


def version_stamp() -> dict[str, str]:
    return dict(VERSIONS)
