"""Template configuration loader. `default.toml` is the base; a template file overrides keys."""
from __future__ import annotations

import tomllib
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / "templates" / "default.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(template: str | Path | None = None) -> dict:
    cfg = tomllib.loads(DEFAULT.read_text(encoding="utf-8"))
    if template is not None:
        cfg = _merge(cfg, tomllib.loads(Path(template).read_text(encoding="utf-8")))
    return cfg
