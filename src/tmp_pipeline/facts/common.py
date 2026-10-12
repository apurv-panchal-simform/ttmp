"""Small helpers shared by the field mapper and the checks."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..catalog import normalise
from ..config import FactsCfg
from ..records import Record, Status
from .model import FactValue, Source

# record kinds whose text is never scanned for references: the drawings and figures describe themselves
NOT_SCANNED = {"diagram_sheet", "figure", "risk_cell", "risk_matrix", "risk_impact_key"}


def source_of(record: Record, raw: str | None = None) -> Source:
    return Source(record.record_id, record.page, record.element_ids(), raw)


def norm_name(text: str, cfg: FactsCfg) -> str:
    """Names compared for merging: lower case, punctuation collapsed, abbreviations expanded ("Ave" -> "avenue")."""
    return " ".join(cfg.name_abbreviations.get(w, w) for w in normalise(text).split())


def same(a: Any, b: Any, cfg: FactsCfg) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, str) and isinstance(b, str):
        return norm_name(a, cfg) == norm_name(b, cfg)
    return bool(a == b)


def day_type(text: str | None, cfg: FactsCfg) -> str | None:
    if not text:
        return None
    return next((k for k, rx in cfg.day_types.items() if rx.search(text)), None)


def combine_status(values: Iterable[FactValue]) -> Status:
    sts = [v.status for v in values]
    if any(s == Status.CONFLICT for s in sts):
        return Status.CONFLICT
    if sts and all(s == Status.NOT_APPLICABLE for s in sts):
        return Status.NOT_APPLICABLE
    if any(s == Status.UNVERIFIED for s in sts):
        return Status.UNVERIFIED
    return Status.VERIFIED


def record_texts(record: Record) -> list[tuple[str, str | None]]:
    """The pieces of text a record holds, each with its context (group label or heading path), for the reference
    and mention scanners. Diagram sheets, figures and risk-matrix records are not scanned."""
    if record.kind in NOT_SCANNED:
        return []
    d = record.data
    out: list[tuple[str, str | None]] = []
    if record.kind == "section":
        path = [str(p) for p in d.get("path", [])]
        if d.get("heading"):
            out.append((str(d["heading"]), " / ".join(path[:-1]) or None))
        if d.get("text"):
            out.append((str(d["text"]), " / ".join(path) or None))
        return out
    if record.kind == "table":
        pairs = d.get("pairs") or []
        for p in pairs:
            out.append((f"{p.get('label') or ''}: {p.get('value') or ''}", p.get("group")))
        if not pairs:
            out += [(str(c.get("text") or ""), None) for c in d.get("cells") or [] if c.get("text")]
        return out
    if record.kind == "table_row":
        cells = sorted(d.get("cells") or [], key=lambda c: c.get("col", 0))
        text = " ".join(str(c.get("text") or "") for c in cells).strip()
        return [(text, None)] if text else []
    if record.kind == "checkbox":
        return [(t, None) for t in (d.get("note"), d.get("label")) if t]
    if record.kind == "yes_no":
        return [(str(d["question"]), None)] if d.get("question") else []
    return [(fv.raw, None) for fv in record.fields.values() if isinstance(fv.raw, str) and fv.raw]
