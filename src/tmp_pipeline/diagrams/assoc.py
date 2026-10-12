"""Association (plan section 2.6): RP label -> nearest sign, TEMPORARY plate -> its speed disc, road -> sign, order.

Nothing here is a guess dressed as a fact: an RP label further than the configured reach from every sign stays
unassigned, and the order of signs along a road is reported along the road axis with its direction rule written down
("RP rising"), because the direction of travel for an approach is not recoverable from the vectors alone (X8 and the
gold set cover that).
"""
from __future__ import annotations

import math

from ..config import DiagramsCfg
from ..geometry import centre
from ..vocab import SymbolKind
from .models import Symbol
from .symbols import _gap

SIGN_KINDS = (SymbolKind.SPEED_LIMIT, SymbolKind.TEMPORARY_SPEED_LIMIT, SymbolKind.SPEED_AHEAD_SIGN,
              SymbolKind.WARNING_SIGN, SymbolKind.ADVANCE_SIGN, SymbolKind.SCHOOL_ZONE, SymbolKind.END_OF_WORKS_SIGN,
              SymbolKind.SUPPLEMENTARY_PLATE, SymbolKind.BUS_STOP_SIGN, SymbolKind.PICTOGRAM)
STACK_GAP_PT = 3.0


def _dist(a: Symbol, b: Symbol) -> float:
    return _gap(a.bbox, b.bbox)


def link_temporary_plates(symbols: list[Symbol], cfg: DiagramsCfg) -> None:
    """Each TEMPORARY plate makes its nearest unclaimed speed disc a temporary speed limit."""
    discs = [s for s in symbols if s.kind == SymbolKind.SPEED_LIMIT]
    taken: set[int] = set()
    for plate in [s for s in symbols if s.kind == SymbolKind.SUPPLEMENTARY_PLATE and s.text.strip().upper() == "TEMPORARY"]:
        cand = [(abs(_dist(plate, d)) + 0.001 * math.dist(centre(plate.bbox), centre(d.bbox)), i, d)
                for i, d in enumerate(discs) if i not in taken and _dist(plate, d) <= cfg.plate_assoc_max_pt]
        if cand:
            _, i, d = min(cand, key=lambda c: (c[0], c[1]))
            taken.add(i)
            d.kind, d.temporary = SymbolKind.TEMPORARY_SPEED_LIMIT, True
            d.extra["plate"] = plate.id


def link_rp_labels(symbols: list[Symbol], cfg: DiagramsCfg) -> None:
    signs = [s for s in symbols if s.kind in SIGN_KINDS]
    for lab in [s for s in symbols if s.kind == SymbolKind.RP_LABEL]:
        near = sorted(((_dist(lab, s), s.objects[0] if s.objects else 0, s) for s in signs), key=lambda t: t[:2])
        if not near or near[0][0] > cfg.rp_assoc_max_pt:
            continue
        sign = near[0][2]
        _assign_rp(sign, lab, inherited=False)
        for other in signs:                           # signs stacked against it (plate + disc) share its RP
            if other is not sign and other.rp is None and _dist(sign, other) <= STACK_GAP_PT:
                _assign_rp(other, lab, inherited=True)


def _assign_rp(sign: Symbol, label: Symbol, inherited: bool) -> None:
    sign.rp, sign.rp_id = float(label.value), label.id            # type: ignore[arg-type]
    if inherited:
        sign.extra["rp_inherited"] = True


def link_roads(symbols: list[Symbol]) -> list[dict[str, str]]:
    roads = [s for s in symbols if s.kind == SymbolKind.ROAD_LABEL]
    for s in symbols:
        if s.kind in SIGN_KINDS and roads:
            r = min(roads, key=lambda lab: math.dist(centre(s.bbox), centre(lab.bbox)))
            s.road, s.road_level = str(r.value).title(), r.meaning
    seen: dict[tuple[str, str], None] = {}
    for r in roads:
        seen[(str(r.value).title(), r.meaning or "")] = None
    return [{"name": n, "level": lv} for n, lv in seen]


def order_along_roads(symbols: list[Symbol]) -> None:
    """Number the signs on each road along its axis, oriented so that RP rises; flag the direction as undecided."""
    by_road: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.kind in SIGN_KINDS and s.kind not in (SymbolKind.SUPPLEMENTARY_PLATE,) and s.road:
            by_road.setdefault(s.road, []).append(s)
    for road, group in by_road.items():
        if len(group) < 2:
            for s in group:
                s.order, s.approach = 1, f"{road} (RP rising)"
            continue
        pts = [centre(s.bbox) for s in group]
        mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        syy = sum((p[1] - my) ** 2 for p in pts)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
        theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
        ux, uy = math.cos(theta), math.sin(theta)
        proj = {id(s): (p[0] - mx) * ux + (p[1] - my) * uy for s, p in zip(group, pts, strict=True)}
        with_rp = [(proj[id(s)], s.rp) for s in group if s.rp is not None]
        if len(with_rp) >= 2:
            mean_p = sum(p for p, _ in with_rp) / len(with_rp)
            mean_r = sum(r for _, r in with_rp) / len(with_rp)
            if sum((p - mean_p) * (r - mean_r) for p, r in with_rp) < 0:
                proj = {k: -v for k, v in proj.items()}
        for n, s in enumerate(sorted(group, key=lambda s: proj[id(s)]), 1):
            s.order, s.approach = n, f"{road} (RP rising)"
            s.extra["direction_of_travel"] = "undecided"


def link_all(symbols: list[Symbol], cfg: DiagramsCfg) -> list[dict[str, str]]:
    link_temporary_plates(symbols, cfg)
    link_rp_labels(symbols, cfg)
    roads = link_roads(symbols)
    order_along_roads(symbols)
    return roads
