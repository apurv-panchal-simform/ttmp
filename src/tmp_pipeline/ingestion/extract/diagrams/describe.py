"""What a diagram sheet shows, from its vector and text layers alone (no model): the facts image questions need.

Every item on the sheet with a plain label and a position ("top-left" ... "bottom-right" of the plan, or inside the
inset or a picture), the order of the plan items from left to right, every distance with the items at the two ends of
its dimension line, and every piece of text as printed, flagged when the font encoding garbled it. A model reading
(X13) adds appearance, meaning and description on top of these, checked against them (``model.validators.check_x13``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...geometry import centre
from .models import SheetModel, Symbol

_SKIP = {"line"}                                  # unlabelled linework: never an item on its own
_POINT = {"speed_ahead_sign", "temporary_speed_limit", "speed_limit", "warning_sign", "advance_sign", "school_zone",
          "end_of_works_sign", "supplementary_plate", "bus_stop_sign", "traffic_controller", "pictogram", "rp_label",
          "house_label"}
_SPAN = {"area", "cones"}
_TITLE_FIELDS = {"tmp_number": "TMP number", "sheet": "Sheet no", "project": "Project", "drawn_by": "Drawn & checked by",
                 "checked_by": "Checked by", "scale": "Scale", "rev": "Rev", "date": "Date", "originator": "Company"}


@dataclass
class Item:
    id: str
    kind: str
    label: str
    where: str                       # plan | inset | figure
    location: str                    # "top-left of the plan", "inside the inset (top-right)"
    bbox: list[float]
    road: str | None = None
    approach: str | None = None
    order: int | None = None
    rp: float | None = None
    text: str = ""
    caption_status: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def plan_frame(sheet: SheetModel, page_size: tuple[float, float]) -> list[float]:
    """The drawing area: the aerial photo when there is one, else the page inside the title block."""
    aerial = [i.bbox for i in sheet.images if i.role == "aerial"]
    if aerial:
        return max(aerial, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return [14.0, 96.0, page_size[0] - 14.0, page_size[1] - 10.0]


def locate(bbox: list[float], frame: list[float]) -> str:
    """Position on a 3 x 3 grid of ``frame`` (page points, y up)."""
    cx, cy = centre(bbox)
    fx = (cx - frame[0]) / max(1e-6, frame[2] - frame[0])
    fy = (cy - frame[1]) / max(1e-6, frame[3] - frame[1])
    col = "left" if fx < 1 / 3 else "right" if fx > 2 / 3 else "centre"
    row = "bottom" if fy < 1 / 3 else "top" if fy > 2 / 3 else "middle"
    return "centre" if (row, col) == ("middle", "centre") else f"{row}-{col}" if col != "centre" else f"{row}-centre"


def garbled(text: str, rx: re.Pattern[str]) -> bool:
    return bool(text) and bool(rx.search(text))


def _inside(b: list[float], box: list[float]) -> bool:
    cx, cy = centre(b)
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def label_of(s: Symbol) -> str:
    """A plain name for one symbol, from what the vectors and text say."""
    k, v, t = str(s.kind), s.value, (s.text or "").strip()
    temp = " (temporary)" if s.temporary else ""
    if k in ("speed_limit", "temporary_speed_limit"):
        return f"{'temporary ' if k == 'temporary_speed_limit' else ''}speed limit sign {v} km/h{temp}"
    if k == "speed_ahead_sign":
        return f'"{v} km/h AHEAD" speed warning sign' if v is not None else f'speed warning sign "{t}"'
    if k == "rp_label":
        return f"RP label {v}"
    if k == "dimension_label":
        return f"distance label {v:g} {s.unit or 'm'}" if isinstance(v, int | float) else f"distance label {t}"
    if k == "house_label":
        return f"house #{v}"
    if k == "road_label":
        return f'road name "{t}"'
    if k == "place_label":
        return f'place name "{t}"'
    if k == "callout":
        return f'notice "{t}"'
    if k == "area":
        return f"{(s.meaning or str(v or 'area')).replace('_', ' ')} (shaded area)"
    if k == "cones":
        return f"cone run ({s.count} cone{'s' if s.count != 1 else ''})"
    if k == "line":
        return f"{s.meaning} line" if s.meaning else "line"
    if k == "pictogram":
        cap = (s.extra.get("caption") or {}).get("caption")
        return f"pictogram: {cap}" if cap else "pictogram (meaning not stated)"
    name = k.replace("_", " ")
    return f'{name} "{t}"{temp}' if t else f"{name}{temp}"


def items(sheet: SheetModel, frame: list[float], skip_place: re.Pattern[str]) -> list[Item]:
    figures = [r for r in sheet.regions if r.kind == "figure"]
    insets = [r for r in sheet.regions if r.kind == "inset"]
    out: list[Item] = []
    for s in sheet.symbols:
        k = str(s.kind)
        if k in _SKIP and not s.meaning:
            continue
        if k == "place_label" and skip_place.search(s.text or str(s.value or "")):
            continue
        fig = next((r for r in figures if _inside(s.bbox, r.bbox)), None)
        ins = next((r for r in insets if _inside(s.bbox, r.bbox)), None)
        if s.region == "inset" or ins is not None:
            box = ins.bbox if ins is not None else s.bbox
            where, loc = "inset", f"inside the inset ({locate(box, frame)} of the plan)"
        elif fig is not None:
            where, loc = "figure", f"inside a picture beside the plan ({locate(fig.bbox, frame)})"
        else:
            where, loc = "plan", f"{locate(s.bbox, frame)} of the plan"
        cap = s.extra.get("caption") or {}
        out.append(Item(s.id, k, label_of(s), where, loc, list(s.bbox), s.road, s.approach, s.order, s.rp,
                        (s.text or "").strip(), cap.get("status") if cap else None,
                        {"value": s.value, "unit": s.unit, "count": s.count, "temporary": s.temporary}))
    return out


def left_to_right(its: list[Item]) -> list[str]:
    """Plan items ordered by their centre across the page (notices and names left out: they label, not stand)."""
    keep = [i for i in its if i.where == "plan" and i.kind not in ("callout", "place_label", "road_label")]
    return [i.id for i in sorted(keep, key=lambda i: (centre(i.bbox)[0], -centre(i.bbox)[1]))]


@dataclass
class Distance:
    id: str
    value: float | int | None
    unit: str
    location: str
    span: list[float]
    vertical: bool
    start: str | None = None         # what sits at the left (or bottom) end of its dimension line
    end: str | None = None           # ...and at the right (or top) end
    before: str | None = None        # the dimension that ends where this one starts, if any
    after: str | None = None


def _end_match(x: float, axis: int, line_c: float, its: list[Item], tol: float, max_d: float) -> str | None:
    """The item whose edge (shaded areas, cone runs) or centre (signs, labels) meets one end of a dimension line."""
    best: tuple[float, str] | None = None
    for i in its:
        if i.where != "plan" or i.kind not in (_POINT | _SPAN):
            continue
        c = centre(i.bbox)
        if abs(c[1 - axis] - line_c) > max_d:
            continue
        if i.kind in _SPAN:
            lo, hi = i.bbox[axis], i.bbox[axis + 2]
            for pos, side in ((lo, "start"), (hi, "end")):
                d = abs(pos - x)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, f"the {side} of the {i.label}")
        else:
            d = abs(c[axis] - x)
            if d <= tol and (best is None or d < best[0]):
                best = (d, f"the {i.label}")
    return best[1] if best else None


def distances(sheet: SheetModel, its: list[Item], frame: list[float], tol: float, max_d: float) -> list[Distance]:
    dims = [s for s in sheet.symbols if str(s.kind) == "dimension_label"]
    out: list[Distance] = []
    for s in dims:
        span = list(s.extra.get("span") or s.bbox)
        vertical = (span[3] - span[1]) > 1.5 * (span[2] - span[0])
        axis = 1 if vertical else 0
        line_c = centre(span)[1 - axis]
        d = Distance(s.id, s.value if isinstance(s.value, int | float) else None, s.unit or "m",
                     f"{locate(s.bbox, frame)} of the plan" if s.region != "inset" else "inside the inset", span, vertical)
        d.start = _end_match(span[axis], axis, line_c, its, tol, max_d)
        d.end = _end_match(span[axis + 2], axis, line_c, its, tol, max_d)
        out.append(d)
    for d in out:
        axis = 1 if d.vertical else 0
        for o in out:
            if o is d or o.vertical != d.vertical:
                continue
            if abs(o.span[axis + 2] - d.span[axis]) <= tol and abs(centre(o.span)[1 - axis] - centre(d.span)[1 - axis]) <= max_d:
                d.before = o.id
            if abs(o.span[axis] - d.span[axis + 2]) <= tol and abs(centre(o.span)[1 - axis] - centre(d.span)[1 - axis]) <= max_d:
                d.after = o.id
    return sorted(out, key=lambda d: (d.vertical, d.span[1] if d.vertical else d.span[0]))


def texts(sheet: SheetModel, its: list[Item], frame: list[float], rx: re.Pattern[str]) -> list[dict]:
    """Every piece of text on the sheet as printed, where it is, and whether its font encoding garbled it."""
    out: list[dict] = []

    def add(text: str, where: str, location: str) -> None:
        text = " ".join(str(text).split())
        if text and not any(t["text"] == text and t["where"] == where for t in out):
            out.append({"text": text, "where": where, "location": location, "garbled": garbled(text, rx)})

    for k, v in sheet.title_block.items():
        add(f"{_TITLE_FIELDS.get(k, k.replace('_', ' ').capitalize())}: {v}", "title block", "bottom strip of the sheet")
    regions = {r.kind: r for r in sheet.regions}
    for e in sheet.legend:
        add(e.meaning, "legend", f"legend ({locate(regions['legend'].bbox, frame)})" if "legend" in regions else "legend")
    for n in sheet.notes:
        add(n, "notes", f"notes panel ({locate(regions['notes'].bbox, frame)})" if "notes" in regions else "notes panel")
    for i in its:
        if i.text:
            add(i.text, i.where, i.location)
    for r in sheet.regions:
        if r.kind == "figure" and r.label:
            add(r.label, "figure title", f"title of a picture beside the plan ({locate(r.bbox, frame)})")
    for ln in sheet.low_quality_lines:
        add(ln, "sheet", "printed on the sheet (position not recorded)")
    return out


def describe(sheet: SheetModel, page_size: tuple[float, float], cfg: Any) -> dict:
    """The code facts for one sheet, as plain data for its record (``data["describe"]["facts"]``)."""
    frame = plan_frame(sheet, page_size)
    its = items(sheet, frame, cfg.skip_place)
    dists = distances(sheet, its, frame, cfg.end_tol_pt, cfg.end_max_d_pt)
    return {
        "frame": frame,
        "items": [vars(i) for i in its],
        "left_to_right": left_to_right(its),
        "distances": [vars(d) for d in dists],
        "texts": texts(sheet, its, frame, cfg.garbled),
        "cones": [{"id": i.id, "count": i.extra.get("count"), "location": i.location} for i in its if i.kind == "cones"],
    }
