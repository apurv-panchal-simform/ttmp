"""Router (plan section 1.6): plain code, no prompt.

``decide`` is a pure function from the store to one ``Decision`` per element; ``apply_decisions`` writes the
results back; ``route`` then hands every ``sent`` element to its handler and logs one line per element.

Decisions come from small named rules in ordered phases. A rule returns a Decision or None, and the first
rule that answers wins, so adding a route means adding a rule, not editing a loop:
  structure  page furniture -> metadata; zone regions and diagram sheets are kept as structure
  container  figures (triaged by rule) and tables, which then claim the text inside them
  item       checkboxes, text claimed by a container, free text, and anything unclaimed -> LEFTOVER

Ownership: a checkbox is always owned by the CHECKBOX handler, even inside a table (its table is context,
recorded in ``claimed_by``, and the table handler skips checkbox children). Checkboxes on a diagram sheet or
inside a figure belong to that sheet or figure instead. Unclear figures are logged "needs_x1" and treated as
informative; the X1 model call is not part of Phase 1.
"""
from __future__ import annotations

import json
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import logging_ as lg
from .catalog import Catalog, load_catalog
from .config import Config
from .contracts import Handler, HandlerContext, HandlerRegistry, HandlerResult
from .geometry import centre_in, iou
from .handlers.stubs import stub_registry
from .models import Element, ElementStore
from .records import RecordStore
from .versions import VERSIONS, version_stamp
from .vocab import ElementType, Flag, Outcome, Route, Zone

_CONTAINER_ROUTES = (Route.TABLE, Route.RISK_TABLE, Route.DIAGRAM)


@dataclass(frozen=True)
class Decision:
    route: Route
    rule: str
    outcome: Outcome
    triage: str | None = None
    claimed_by: str | None = None
    set_parent: str | None = None          # parent_id to give the element when it has none
    add_flags: tuple[Flag, ...] = ()


class RouteContext:
    """Everything a rule may look at; built once per ``decide`` call and never mutated by rules."""

    def __init__(self, store: ElementStore, cfg: Config):
        self.store, self.cfg = store, cfg
        self.out: dict[str, Decision] = {}
        self.sheet_of_page = {e.page: e for e in store.of_type(ElementType.DIAGRAM_SHEET)}
        self.repeating = _repeating_images(store, cfg)
        self.triage = {im.element_id: _triage(im, store, cfg, self.repeating) for im in store.of_type(ElementType.IMAGE)}
        self.figures = [im for im in store.of_type(ElementType.IMAGE)
                        if im.page not in self.sheet_of_page and self.triage[im.element_id][0] != "decorative"]


Rule = Callable[[Element, RouteContext], Decision | None]


def _repeating_images(store: ElementStore, cfg: Config) -> set[str]:
    """Images that sit in the same spot, at the same size, on more than the configured share of pages."""
    imgs = store.of_type(ElementType.IMAGE)
    n_pages = len(store.pages)
    f = cfg.figures
    out: set[str] = set()
    for im in imgs:
        pages = {o.page for o in imgs if iou(im.bbox_pt, o.bbox_pt) >= f.repeat_iou and o.page_size_pt == im.page_size_pt}
        if len(pages) > f.repeat_page_share * n_pages and len(pages) >= f.repeat_min_pages:
            out.add(im.element_id)
    return out


def _triage(im: Element, store: ElementStore, cfg: Config, repeating: set[str]) -> tuple[str, str]:
    f = cfg.figures
    if im.element_id in repeating:
        return "decorative", "figure.repeats_on_most_pages"
    share = im.metadata["area_share"]
    has_text = any(e.type in (ElementType.TEXT_BLOCK, ElementType.PAGE_FURNITURE) and e.page == im.page
                   and centre_in(e.bbox_pt, im.bbox_pt) for e in store.on_page(im.page))
    if has_text:
        return "informative", "figure.has_text"
    if share < f.decorative_max_area:
        return "decorative", "figure.small_no_text"
    if share >= f.informative_min_area:
        return "informative", "figure.large"
    return "needs_x1", "figure.unclear_size"


# --- structure phase -------------------------------------------------------------------------------------------

def rule_furniture(e: Element, c: RouteContext) -> Decision | None:
    if e.type == ElementType.PAGE_FURNITURE:
        return Decision(Route.METADATA, f"furniture.{e.metadata.get('furniture', 'band')}", Outcome.METADATA)
    return None


def rule_structure(e: Element, c: RouteContext) -> Decision | None:
    if e.type in (ElementType.REGION, ElementType.DIAGRAM_SHEET):
        return Decision(Route.STRUCTURE, f"zones.{e.type}", Outcome.KEPT)
    return None


# --- container phase -------------------------------------------------------------------------------------------

def rule_image(e: Element, c: RouteContext) -> Decision | None:
    if e.type != ElementType.IMAGE:
        return None
    triage, rule = c.triage[e.element_id]
    if triage == "decorative":
        return Decision(Route.DECORATIVE, rule, Outcome.SKIPPED, triage, add_flags=(Flag.DECORATIVE,))
    sheet = c.sheet_of_page.get(e.page)
    if sheet is not None:
        return Decision(Route.DIAGRAM, "zone.diagram", Outcome.CLAIMED, triage, sheet.element_id,
                        set_parent=sheet.element_id)
    return Decision(Route.FIGURE, rule, Outcome.SENT, triage)


def rule_table(e: Element, c: RouteContext) -> Decision | None:
    if e.type != ElementType.TABLE:
        return None
    sheet = c.sheet_of_page.get(e.page)
    if sheet is not None:
        return Decision(Route.DIAGRAM, "zone.diagram", Outcome.CLAIMED, None, sheet.element_id,
                        set_parent=sheet.element_id)
    if e.zone == Zone.RISK:
        return Decision(Route.RISK_TABLE, "table.zone_risk", Outcome.SENT)
    return Decision(Route.TABLE, "table.ruled_grid", Outcome.SENT)


# --- item phase ------------------------------------------------------------------------------------------------

def _figure_containing(e: Element, c: RouteContext) -> Element | None:
    return next((f for f in c.figures if f.page == e.page and centre_in(e.bbox_pt, f.bbox_pt)), None)


def rule_checkbox(e: Element, c: RouteContext) -> Decision | None:
    if e.type != ElementType.CHECKBOX:
        return None
    sheet = c.sheet_of_page.get(e.page)
    if sheet is not None:
        return Decision(Route.DIAGRAM, "zone.diagram", Outcome.CLAIMED, None, sheet.element_id,
                        set_parent=sheet.element_id)
    fig = _figure_containing(e, c)
    if fig is not None:
        return Decision(Route.FIGURE, "claimed.inside_figure", Outcome.CLAIMED, None, fig.element_id,
                        set_parent=fig.element_id)
    return Decision(Route.CHECKBOX, "checkbox.detected", Outcome.SENT, claimed_by=e.parent_id)


def rule_claimed_by_container(e: Element, c: RouteContext) -> Decision | None:
    parent = c.out.get(e.parent_id) if e.parent_id else None
    if parent is None or parent.route not in _CONTAINER_ROUTES:
        return None
    sheet = c.sheet_of_page.get(e.page)
    on_sheet = sheet is not None and e.parent_id == sheet.element_id
    return Decision(parent.route, "zone.diagram" if on_sheet else "claimed.inside_table", Outcome.CLAIMED,
                    claimed_by=e.parent_id)


def rule_on_sheet(e: Element, c: RouteContext) -> Decision | None:
    sheet = c.sheet_of_page.get(e.page)
    if sheet is None:
        return None
    return Decision(Route.DIAGRAM, "zone.diagram", Outcome.CLAIMED, None, sheet.element_id, set_parent=sheet.element_id)


def rule_inside_figure(e: Element, c: RouteContext) -> Decision | None:
    fig = _figure_containing(e, c)
    if fig is None:
        return None
    return Decision(Route.FIGURE, "claimed.inside_figure", Outcome.CLAIMED, None, fig.element_id,
                    set_parent=fig.element_id)


def rule_text(e: Element, c: RouteContext) -> Decision | None:
    if e.type == ElementType.TEXT_BLOCK and e.zone != Zone.UNCLASSIFIED:
        return Decision(Route.TEXT, f"text.zone_{e.zone}", Outcome.SENT)
    return None


def rule_leftover(e: Element, c: RouteContext) -> Decision | None:
    rule = "unclaimed" if e.type != ElementType.TEXT_BLOCK else "zone.unclassified"
    return Decision(Route.LEFTOVER, rule, Outcome.LEFTOVER, add_flags=(Flag.LEFTOVER,))


PHASES: tuple[tuple[str, tuple[Rule, ...]], ...] = (
    ("structure", (rule_furniture, rule_structure)),
    ("container", (rule_image, rule_table)),
    ("item", (rule_checkbox, rule_claimed_by_container, rule_on_sheet, rule_inside_figure, rule_text, rule_leftover)),
)


def decide(store: ElementStore, cfg: Config) -> dict[str, Decision]:
    """Pure: one Decision per element, no change to the store."""
    ctx = RouteContext(store, cfg)
    for _name, rules in PHASES:
        for e in store.elements.values():
            if e.element_id in ctx.out:
                continue
            for rule in rules:
                d = rule(e, ctx)
                if d is not None:
                    ctx.out[e.element_id] = d
                    break
    for e in store.elements.values():            # a generic standard is not this site's fact
        if e.zone == Zone.GENERIC_STANDARD and Flag.GENERIC not in e.flags:
            d = ctx.out[e.element_id]
            ctx.out[e.element_id] = Decision(d.route, d.rule, d.outcome, d.triage, d.claimed_by, d.set_parent,
                                             (*d.add_flags, Flag.GENERIC))
    return ctx.out


def apply_decisions(store: ElementStore, decisions: dict[str, Decision]) -> None:
    for eid, d in decisions.items():
        e = store.elements[eid]
        e.route, e.route_rule, e.outcome, e.triage, e.claimed_by = d.route, d.rule, d.outcome, d.triage, d.claimed_by
        if d.set_parent and not e.parent_id:
            e.parent_id = d.set_parent
        if d.add_flags:
            e.flags = sorted({*e.flags, *d.add_flags})


@dataclass
class RouterResult:
    decisions: dict[str, Decision]
    records: list[dict]
    failures: list[dict]

    def coverage(self, store: ElementStore) -> dict:
        logged = Counter(r["element"] for r in self.records)
        missing = [eid for eid in store.elements if eid not in logged]
        duplicated = [eid for eid, n in logged.items() if n > 1]
        unknown = [eid for eid in logged if eid not in store.elements]
        total = len(store.elements)
        return {"elements": total, "logged": len(self.records), "missing": missing, "duplicated": duplicated,
                "unknown": unknown, "failed": len(self.failures),
                "coverage": (total - len(missing)) / total if total else 1.0}


def _apply_result(ctx: HandlerContext, res: HandlerResult) -> None:
    for rec in res.records:
        ctx.records.add(rec)
    for eid, upd in res.element_updates.items():
        e = ctx.store.elements[eid]
        if upd.add_flags:
            e.flags = sorted({*e.flags, *upd.add_flags})
        e.metadata.update(upd.metadata)


def route(store: ElementStore, cfg: Config, log_path: str | Path | None = None,
          registry: HandlerRegistry | None = None, records: RecordStore | None = None, model=None,
          catalog: Catalog | None = None) -> RouterResult:
    """Decide, apply, and run the handlers. A handler that raises fails only its own element."""
    registry = registry if registry is not None else stub_registry()
    decisions = decide(store, cfg)
    registry.require({d.route for d in decisions.values() if d.outcome == Outcome.SENT})
    apply_decisions(store, decisions)
    ctx = HandlerContext(store, records if records is not None else RecordStore(store.doc_id), cfg,
                         version_stamp(), model, catalog or load_catalog())
    out_records: list[dict] = []
    failures: list[dict] = []
    used: list[Handler] = []
    try:
        for h in registry:
            if any(d.route == h.route and d.outcome == Outcome.SENT for d in decisions.values()):
                used.append(h)
                h.begin(ctx)
        for e in store.elements.values():
            d = decisions[e.element_id]
            error = None
            if d.outcome == Outcome.SENT:
                try:
                    res = registry.get(d.route).handle(ctx, e)
                    _apply_result(ctx, res)
                    if res.outcome is not None:
                        e.outcome = res.outcome
                except Exception as exc:         # isolate: one bad element must not lose the rest of the run
                    error = f"{type(exc).__name__}: {exc}"
                    e.outcome = Outcome.FAILED
                    failures.append({"element": e.element_id, "route": str(d.route), "error": error,
                                     "trace": traceback.format_exc()})
                    lg.log_event("router", "handler_failed", element=e.element_id, route=str(d.route), error=error)
            out_records.append({
                "doc_id": store.doc_id, "run_id": lg.get_run_id(), "router_version": VERSIONS["router"],
                "element": e.element_id, "page": e.page, "type": str(e.type), "zone": str(e.zone),
                "route": str(d.route), "rule": d.rule, "triage": d.triage, "outcome": str(e.outcome),
                "claimed_by": d.claimed_by, "error": error})
        for h in used:
            try:
                _apply_result(ctx, h.finish(ctx))
            except Exception as exc:
                failures.append({"element": None, "route": str(h.route), "error": f"{type(exc).__name__}: {exc}",
                                 "trace": traceback.format_exc()})
                lg.log_event("router", "handler_finish_failed", route=str(h.route), error=str(exc))
    finally:
        if log_path is not None:               # a crash must not lose the log of what was routed so far
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_records), encoding="utf-8")
    res_obj = RouterResult(decisions, out_records, failures)
    cov = res_obj.coverage(store)
    lg.log_event("router", "done", elements=cov["elements"], coverage=cov["coverage"], failed=cov["failed"],
                 routes=dict(Counter(r["route"] for r in out_records)))
    return res_obj
