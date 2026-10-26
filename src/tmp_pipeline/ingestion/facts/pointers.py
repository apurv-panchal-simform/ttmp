"""Pointers (plan section 3.2): "See Above" resolves to the nearest earlier place of the matching type in the same
form, stored as a link to those facts, never as a copy. ``[facts.pointer_targets]`` says where each field points.
A pointer that cannot be resolved stays unverified with the reason.
"""
from __future__ import annotations

from ...config import FactsCfg
from ...records import Status
from ..models import ElementStore
from .model import Facts

REFERS_NOTE = "refers elsewhere"


def _form(store: ElementStore, page: int) -> str | None:
    info = store.pages.get(page)
    return info.form_name if info else None


def resolve_pointers(facts: Facts, store: ElementStore, cfg: FactsCfg) -> list[str]:
    """Resolve every pointer in place; returns the paths that stayed unresolved."""
    unresolved = []
    for fact in facts.facts:
        for fname, fv in fact.fields.items():
            if fv.value is not None or not any(REFERS_NOTE in n for n in fv.notes):
                continue
            path = f"{fact.fact_id}.{fname}"
            target = cfg.pointer_targets.get(f"{fact.entity}.{fname}")
            if target is None:
                fv.status = Status.UNVERIFIED
                fv.notes.append("no pointer target is configured for this field")
                unresolved.append(path)
                continue
            tentity, _, tfield = target.partition(".")
            page = min(s.page for s in fv.sources)
            form = _form(store, page)
            candidates = [(s.page, t) for t in facts.of(tentity) if tfield in t.fields for s in t.sources
                          if s.page <= page and _form(store, s.page) == form]
            if not candidates:
                fv.status = Status.UNVERIFIED
                fv.notes.append(f"no earlier {tentity} in the same form to refer to")
                unresolved.append(path)
                continue
            nearest = max(p for p, _ in candidates)
            fv.link = sorted({f"{t.fact_id}.{tfield}" for p, t in candidates if p == nearest})
            fv.method = "pointer"
            fv.notes.append(f"resolved to {tentity}.{tfield} on page {nearest}")
    return unresolved
