"""Router (plan section 1.6): plain code, no prompt.

Sends each element to a handler by type, in this order, and writes one log line per element:
  1. page furniture -> metadata; zones' own region / sheet elements are kept as structure
  2. tables and figures first, claiming the text inside them so nothing is stored twice
  3. checkboxes (paired with labels by the checkbox handler)
  4. remaining text
  5. anything unclaimed -> LEFTOVER: flagged, becomes an other_section chunk later

Figure triage is by rule. Unclear figures are logged "needs_x1" and treated as informative; the X1 model
call is not part of Phase 1.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import logging_ as lg
from .handlers.stubs import default_registry
from .models import Element, ElementStore
from .versions import VERSIONS


@dataclass
class Decision:
    route: str
    rule: str
    outcome: str
    triage: str | None = None
    claimed_by: str | None = None


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _centre_in(inner, outer) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _repeating_images(store: ElementStore, cfg: dict) -> set[str]:
    """Images that sit in the same spot, at the same size, on more than half the pages."""
    imgs = store.of_type("image")
    n_pages = len(store.pages)
    out: set[str] = set()
    for im in imgs:
        pages = {o.page for o in imgs if _iou(im.bbox_pt, o.bbox_pt) >= cfg["figures"]["repeat_iou"]
                 and o.page_size_pt == im.page_size_pt}
        if len(pages) > 0.5 * n_pages and len(pages) >= cfg["figures"]["repeat_min_pages"]:
            out.add(im.element_id)
    return out


def _triage(im: Element, store: ElementStore, cfg: dict, repeating: set[str]) -> tuple[str, str]:
    f = cfg["figures"]
    if im.element_id in repeating:
        return "decorative", "figure.repeats_on_most_pages"
    share = im.metadata["area_share"]
    has_text = any(e.type in ("text_block", "page_furniture") and e.page == im.page
                   and _centre_in(e.bbox_pt, im.bbox_pt) for e in store.on_page(im.page))
    if has_text:
        return "informative", "figure.has_text"
    if share < f["decorative_max_area"]:
        return "decorative", "figure.small_no_text"
    if share >= f["informative_min_area"]:
        return "informative", "figure.large"
    return "needs_x1", "figure.unclear_size"


def decide(store: ElementStore, cfg: dict) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    repeating = _repeating_images(store, cfg)
    sheet_of_page = {e.page: e for e in store.of_type("diagram_sheet")}
    for e in store.elements.values():            # a generic standard is not this site's fact
        if e.zone == "generic_standard" and "generic" not in e.flags:
            e.flags = sorted({*e.flags, "generic"})

    def set_(e: Element, route, rule, outcome, triage=None, claimed_by=None):
        out[e.element_id] = Decision(route, rule, outcome, triage, claimed_by)

    # 1. furniture and structure
    for e in store.elements.values():
        if e.type == "page_furniture":
            set_(e, "METADATA", f"furniture.{e.metadata.get('furniture', 'band')}", "metadata")
        elif e.type in ("region", "diagram_sheet"):
            set_(e, "STRUCTURE", f"zones.{e.type}", "kept")
    # 2. figures and tables (claiming what is inside)
    for e in store.elements.values():
        if e.element_id in out:
            continue
        sheet = sheet_of_page.get(e.page)
        if e.type == "image":
            triage, rule = _triage(e, store, cfg, repeating)
            if triage == "decorative":
                e.flags = sorted({*e.flags, "decorative"})
                set_(e, "DECORATIVE", rule, "skipped", triage)
            elif sheet is not None:
                e.parent_id = e.parent_id or sheet.element_id
                set_(e, "DIAGRAM", "zone.diagram", "claimed", triage, sheet.element_id)
            else:
                set_(e, "FIGURE", rule, "sent", triage)
        elif e.type == "table":
            if sheet is not None:
                e.parent_id = e.parent_id or sheet.element_id
                set_(e, "DIAGRAM", "zone.diagram", "claimed", None, sheet.element_id)
            elif e.zone == "risk":
                set_(e, "RISK_TABLE", "table.zone_risk", "sent")
            else:
                set_(e, "TABLE", "table.ruled_grid", "sent")
    # 3. checkboxes; 4. remaining text; 5. leftover
    figures = [e for e in store.of_type("image") if out[e.element_id].route == "FIGURE"]
    for e in store.elements.values():
        if e.element_id in out:
            continue
        sheet = sheet_of_page.get(e.page)
        parent = store.elements.get(e.parent_id) if e.parent_id else None
        if e.type == "checkbox":
            set_(e, "CHECKBOX", "checkbox.detected", "sent", claimed_by=parent.element_id if parent else None)
            continue
        if parent is not None and out.get(parent.element_id) and out[parent.element_id].route in ("TABLE", "RISK_TABLE", "DIAGRAM"):
            set_(e, out[parent.element_id].route, "claimed.inside_table" if e.parent_id != (sheet.element_id if sheet else None) else "zone.diagram",
                 "claimed", claimed_by=parent.element_id)
            continue
        if sheet is not None:
            e.parent_id = e.parent_id or sheet.element_id
            set_(e, "DIAGRAM", "zone.diagram", "claimed", claimed_by=sheet.element_id)
            continue
        inside = next((f for f in figures if f.page == e.page and _centre_in(e.bbox_pt, f.bbox_pt)), None)
        if inside is not None:
            e.parent_id = e.parent_id or inside.element_id
            set_(e, "FIGURE", "claimed.inside_figure", "claimed", claimed_by=inside.element_id)
            continue
        if e.type == "text_block" and e.zone != "unclassified":
            set_(e, "TEXT", f"text.zone_{e.zone}", "sent")
            continue
        e.flags = sorted({*e.flags, "leftover"})
        set_(e, "LEFTOVER", "unclaimed" if e.type != "text_block" else "zone.unclassified", "leftover")
    return out


@dataclass
class RouterResult:
    decisions: dict[str, Decision]
    records: list[dict]

    def coverage(self, store: ElementStore) -> dict:
        logged = Counter(r["element"] for r in self.records)
        missing = [eid for eid in store.elements if eid not in logged]
        duplicated = [eid for eid, n in logged.items() if n > 1]
        unknown = [eid for eid in logged if eid not in store.elements]
        total = len(store.elements)
        return {"elements": total, "logged": len(self.records), "missing": missing, "duplicated": duplicated,
                "unknown": unknown, "coverage": (total - len(missing)) / total if total else 1.0}


def route(store: ElementStore, cfg: dict, log_path: str | Path | None = None, registry=None) -> RouterResult:
    registry = registry if registry is not None else default_registry()
    decisions = decide(store, cfg)
    records: list[dict] = []
    for e in store.elements.values():
        d = decisions[e.element_id]
        e.metadata.update(route=d.route, triage=d.triage, router_outcome=d.outcome)
        if d.outcome == "sent" and d.route in registry:
            registry[d.route].handle(e)
        records.append({
            "doc_id": store.doc_id, "run_id": lg.get_run_id(), "router_version": VERSIONS["router"],
            "element": e.element_id, "page": e.page, "type": e.type, "zone": e.zone, "route": d.route,
            "rule": d.rule, "triage": d.triage, "outcome": d.outcome, "claimed_by": d.claimed_by})
    if log_path is not None:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    res = RouterResult(decisions, records)
    cov = res.coverage(store)
    lg.log_event("router", "done", elements=cov["elements"], coverage=cov["coverage"],
                 routes=dict(Counter(r["route"] for r in records)))
    return res
