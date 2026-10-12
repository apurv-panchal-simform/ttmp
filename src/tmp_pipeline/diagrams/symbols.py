"""Symbols from the vector overlay (plan section 2.6).

A sign is a short run of consecutive drawn objects that sit on top of each other (plate + glyphs, or disc + ring +
digits). Objects are grouped in drawing order; a group is then classified from its text, colours and the legend.
Linework, area fills and cones stand alone. Nothing here guesses: a group that matches no cue is a pictogram and
is marked ``needs_caption``, a labelled box with no known cue is a place label.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import DiagramsCfg
from ..geometry import area, centre, centre_in, intersection, union
from ..numerics import extract
from ..vocab import SymbolKind
from .models import LegendEntry, Symbol
from .overlay import OverlayObject
from .textlines import TextLine

_PAD = 0.5
_PLATE_ORANGE = (240, 126, 34)


@dataclass
class _Item:
    index: int                      # drawing-order index (first glyph for text)
    bbox: list[float]
    obj: OverlayObject | None = None
    line: TextLine | None = None


@dataclass
class _Group:
    items: list[_Item] = field(default_factory=list)
    bbox: list[float] = field(default_factory=list)
    solo: str | None = None         # "area" | "cone": never joins or accepts a join
    box: bool = False               # a big plain box (a callout frame): takes text lines only

    def add(self, it: _Item) -> None:
        self.items.append(it)
        self.bbox = union([self.bbox, it.bbox]) if self.bbox else list(it.bbox)

    @property
    def paths(self) -> list[OverlayObject]:
        return [i.obj for i in self.items if i.obj is not None]

    @property
    def lines(self) -> list[TextLine]:
        return [i.line for i in self.items if i.line is not None]


def _padded(b: list[float]) -> list[float]:
    return [b[0] - _PAD, b[1] - _PAD, b[2] + _PAD, b[3] + _PAD]


def _overlap(a: list[float], b: list[float]) -> float:
    pa, pb = _padded(a), _padded(b)
    return intersection(pa, pb) / min(area(pa), area(pb))


def _colour_match(a, b, tol: int = 3) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


class _Legend:
    """Lookups of legend meanings by colour, for areas, cones and coloured lines."""

    def __init__(self, entries: list[LegendEntry], cfg: DiagramsCfg):
        self.entries, self.cfg = entries, cfg

    def area_meaning(self, o: OverlayObject) -> str | None:
        for e in self.entries:
            if e.fill is not None and not e.dashed and _colour_match(o.fill, e.fill) and e.meaning.lower() in self.cfg.area_meanings:
                return e.meaning
        return None

    def is_cone(self, o: OverlayObject) -> bool:
        return any(e.meaning.lower() == "cone" and _colour_match(o.fill, e.fill) for e in self.entries)

    def line_meaning(self, o: OverlayObject) -> str | None:
        for e in self.entries:
            if o.fill is None and e.stroke is not None and e.stroke != (0, 0, 0) and _colour_match(o.stroke, e.stroke) \
                    and o.dashed == e.dashed:
                return e.meaning
        return None


def _is_background(o: OverlayObject, page_w: float, page_h: float) -> bool:
    return area(o.bbox) >= 0.7 * page_w * page_h


def group_objects(items: list[_Item], legend: _Legend, cfg: DiagramsCfg) -> list[_Group]:
    groups: list[_Group] = []
    cur: _Group | None = None
    for it in items:
        o = it.obj
        solo, box = None, False
        if o is not None:
            if legend.area_meaning(o) is not None:
                solo = "area"
            elif o.fill is not None and area(o.bbox) >= cfg.area_min_pt2:
                if o.fill == (255, 255, 255):
                    box = True
                else:
                    solo = "area"
            elif legend.is_cone(o):
                solo = "cone"
            elif o.fill == (255, 255, 255) and o.stroke == (255, 255, 255):
                box = True                        # a label's white backing: only its own text sits on it
        if solo is not None:
            g = _Group(solo=solo)
            g.add(it)
            groups.append(g)
            cur = None
            continue
        target = _find_group(it, groups, cfg)
        if target is not None:
            target.add(it)
            cur = target
            continue
        cur = _Group(box=box)
        cur.add(it)
        groups.append(cur)
    return merge_fragments(groups, cfg)


_LOOKBACK = 6


_LABEL_PATTERNS = ("rp_label", "dimension_label", "road_label", "house_label")


def _is_label(ln: TextLine, cfg: DiagramsCfg) -> bool:
    return any(cfg.patterns[k].match(ln.key) for k in _LABEL_PATTERNS)


def _find_group(it: _Item, groups: list[_Group], cfg: DiagramsCfg) -> _Group | None:
    """The most recent of the last few groups this item sits on (a label's background may follow a dimension's arrows).

    A distance, RP, road or house label never joins a sign it happens to lie over, and a sign's text never joins a label.
    """
    for g in reversed(groups[-_LOOKBACK:]):
        if g.solo:
            continue
        if it.line is not None:
            mine = _is_label(it.line, cfg)
            if any(_is_label(ln, cfg) != mine for ln in g.lines):
                continue
            if centre_in(it.bbox, _padded(g.bbox)):
                return g
        elif not g.box and _overlap(it.bbox, g.bbox) >= cfg.touch_overlap:
            return g
    return None


def merge_fragments(groups: list[_Group], cfg: DiagramsCfg) -> list[_Group]:
    """Icon fragments merge: consecutive small groups that touch are parts of one drawing (a controller figure)."""
    out: list[_Group] = []
    for g in groups:
        prev = out[-1] if out else None
        if (prev is not None and not g.solo and not prev.solo and not g.box and not prev.box
                and area(g.bbox) <= cfg.fragment_max_pt2 and area(prev.bbox) <= cfg.fragment_max_pt2
                and _gap(prev.bbox, g.bbox) <= cfg.fragment_gap_pt):
            for it in g.items:
                prev.add(it)
            continue
        out.append(g)
    return out


def _number(text: str) -> float | int | None:
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    return float(m.group()) if "." in m.group() else int(m.group())


def _classify(g: _Group, legend: _Legend, cfg: DiagramsCfg) -> Symbol:
    paths, lines = g.paths, g.lines
    key = "".join(ln.key for ln in lines)
    text = " ".join(ln.text.strip() for ln in lines)
    colours = sorted({c for p in paths for c in (p.fill, p.stroke) if c is not None})
    base = dict(bbox=g.bbox, text=text, lines=[ln.text.strip() for ln in lines], colours=colours,
                objects=[i.index for i in g.items],
                angle=lines[0].angle if lines else None, low_text_quality=any(ln.low_quality for ln in lines))
    pat = cfg.patterns

    def sym(kind: SymbolKind, **kw) -> Symbol:
        return Symbol(id="", kind=kind, **{**base, **kw})

    if g.solo == "area":
        meaning = legend.area_meaning(paths[0])
        return sym(SymbolKind.AREA, meaning=meaning, value=cfg.area_meanings.get(meaning.lower()) if meaning else None)
    if g.solo == "cone":
        return sym(SymbolKind.CONES, meaning="Cone")
    if lines:
        words = len(text.split())
        if (m := pat["rp_label"].match(key)):
            return sym(SymbolKind.RP_LABEL, value=float(m.group(1)), unit="km")
        if (m := pat["dimension_label"].match(key)):
            label_box = union([ln.bbox for ln in lines])
            return sym(SymbolKind.DIMENSION_LABEL, value=float(m.group(1)) if "." in m.group(1) else int(m.group(1)),
                       unit="m", **{"bbox": label_box, "extra": {"span": g.bbox}})
        if (m := pat["road_label"].match(key)):
            return sym(SymbolKind.ROAD_LABEL, value=text.rsplit("-", 1)[0].strip(), meaning=m.group(2))
        if pat["house_label"].match(key):
            return sym(SymbolKind.HOUSE_LABEL, value=key.lstrip("#"))
        if pat["controller"].match(key):
            return sym(SymbolKind.TRAFFIC_CONTROLLER)
        if paths and words >= cfg.callout_min_words and any(ch.islower() for ch in text):
            return sym(SymbolKind.CALLOUT)
        for kind, rx in cfg.sign_text.items():
            if rx.search(key):
                value: float | int | str | None = None
                unit: str | None = None
                if kind == "speed_ahead_sign":
                    value, unit = _number(" ".join(ln.text for ln in lines if re.fullmatch(r"\s*\d+\s*", ln.text))), "km/h"
                elif kind == "advance_sign":
                    nums = extract(text, ("distance",))
                    value, unit = (nums[0].value, "m") if nums else (None, None)
                return sym(SymbolKind(kind), value=value, unit=unit)
        if pat["speed_value"].match(key) and paths:
            return sym(SymbolKind.SPEED_LIMIT, value=int(key), unit="km/h")
        if any(_colour_match(p.fill, _PLATE_ORANGE) for p in paths):
            return sym(SymbolKind.WARNING_SIGN)
        return sym(SymbolKind.PLACE_LABEL, value=text)
    stroke_only = [p for p in paths if p.fill is None]
    if paths and all(c == (0, 0, 0) for c in colours):          # leaders, dimension lines, fences, arrowheads
        return sym(SymbolKind.LINE)
    if paths and all(p.fill is None or p.fill == p.stroke for p in paths):     # coloured linework, arrowheads included
        return sym(SymbolKind.LINE, meaning=legend.line_meaning(paths[0]))
    if paths and len(stroke_only) == len(paths):
        return sym(SymbolKind.LINE, meaning=legend.line_meaning(paths[0]))
    if all(p.segments <= 5 and area(p.bbox) < 50 for p in paths):
        return sym(SymbolKind.LINE)
    return sym(SymbolKind.PICTOGRAM, needs_caption=True)


def merge_cone_runs(symbols: list[Symbol], cfg: DiagramsCfg) -> list[Symbol]:
    """Consecutive cones drawn close together are one run, not dozens of symbols."""
    out: list[Symbol] = []
    for s in symbols:
        prev = out[-1] if out else None
        if (s.kind == SymbolKind.CONES and prev is not None and prev.kind == SymbolKind.CONES
                and _gap(prev.bbox, s.bbox) <= cfg.cone_run_gap_pt):
            prev.bbox = union([prev.bbox, s.bbox])
            prev.count += s.count
            prev.objects += s.objects
            continue
        out.append(s)
    return out


def _gap(a: list[float], b: list[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


def read_symbols(objects: list[OverlayObject], lines: list[TextLine], legend_entries: list[LegendEntry],
                 excluded: list[list[float]], page_size: tuple[float, float], cfg: DiagramsCfg) -> list[Symbol]:
    """Group, classify and merge the drawn objects outside the excluded regions (legend, notes, title block...)."""
    legend = _Legend(legend_entries, cfg)
    items: list[_Item] = []
    for o in objects:
        if o.kind != "path" or _is_background(o, *page_size) or any(centre_in(o.bbox, r) for r in excluded):
            continue
        items.append(_Item(o.index, o.bbox, obj=o))
    for ln in lines:
        if not (_horizontal(ln) and any(centre_in(ln.bbox, r) for r in excluded)):     # rotated text is plan content
            items.append(_Item(ln.indices[0], ln.bbox, line=ln))
    items.sort(key=lambda it: it.index)
    symbols = [_classify(g, legend, cfg) for g in group_objects(items, legend, cfg)]
    return absorb_controller_parts(merge_cone_runs(symbols, cfg))


def _horizontal(ln: TextLine) -> bool:
    return min(ln.angle % 360.0, 360.0 - ln.angle % 360.0) <= 5.0


def sign_centre(s: Symbol) -> tuple[float, float]:
    return centre(s.bbox)


def absorb_controller_parts(symbols: list[Symbol], reach_pt: float = 12.0) -> list[Symbol]:
    """A controller figure is drawn as many small pieces: parts without text near the paddle belong to the figure."""
    out = list(symbols)
    for ctl in [s for s in symbols if s.kind == SymbolKind.TRAFFIC_CONTROLLER]:
        zone = [ctl.bbox[0] - reach_pt, ctl.bbox[1] - reach_pt, ctl.bbox[2] + reach_pt, ctl.bbox[3] + reach_pt]
        for s in list(out):
            if s is not ctl and s.kind in (SymbolKind.PICTOGRAM, SymbolKind.LINE) and not s.text and centre_in(s.bbox, zone):
                ctl.bbox = union([ctl.bbox, s.bbox])
                ctl.objects += s.objects
                out.remove(s)
    return out
