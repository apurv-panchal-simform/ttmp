"""Checkbox detection chain, first match wins (plan section 2.4): fillable form fields, then tick glyphs,
then vector squares. Pixel density for scans arrives with the scan path (DI track / Phase 2).
Pairing boxes with labels is Phase 2; here every box becomes one checkbox element.
"""
from __future__ import annotations

from dataclasses import dataclass

from .reader import PageLayers


@dataclass
class Tick:
    bbox: list[float]
    ticked: bool
    method: str            # acroform | glyph | vector_square
    name: str = ""


def _inside(inner, outer, pad=0.5) -> bool:
    return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
            and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def detect_ticks(page: PageLayers, cfg: dict) -> list[Tick]:
    t = cfg["ticks"]
    fields = [Tick(f.bbox_pt, bool(f.checked), "acroform", f.name)
              for f in page.fields if f.kind in ("checkbox", "radio")]
    if fields:
        return fields
    ticked, unticked = set(t["ticked"]), set(t["unticked"])
    glyphs = [Tick([c.x0, c.y0, c.x1, c.y1], c.text in ticked, "glyph")
              for c in page.chars if c.text in ticked or c.text in unticked]
    if glyphs:
        return glyphs
    lo, hi = t["square_min_pt"], t["square_max_pt"]
    squares = []
    for r in page.rects:
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if lo <= w <= hi and abs(w - h) <= 1.2 and r.stroke and not r.fill:
            squares.append(r)
    out = []
    marks = page.lines + page.curves
    for s in squares:
        box = [s.x0, s.y0, s.x1, s.y1]
        mark = any(_inside([m.x0, m.y0, m.x1, m.y1], box) for m in marks) or any(
            _inside([r.x0, r.y0, r.x1, r.y1], box) and r is not s for r in page.rects)
        out.append(Tick(box, mark, "vector_square"))
    return out
