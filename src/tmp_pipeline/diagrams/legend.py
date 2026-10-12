"""The legend, decoded from the drawing (plan section 2.6): swatch colour -> meaning.

In drawing order a swatch (one or more paths) is followed by its label, so entries are read as
"paths since the last label, then a text line". The box and the rule under the title are not swatches.
"""
from __future__ import annotations

from ..config import DiagramsCfg
from ..geometry import area, centre_in
from .models import LegendEntry, colour_name
from .overlay import OverlayObject
from .textlines import TextLine


def find_box(title: TextLine, paths: list[OverlayObject]) -> list[float]:
    """The smallest filled or stroked path box that holds the title line's centre."""
    best: list[float] | None = None
    for p in paths:
        if p.kind == "path" and centre_in(title.bbox, p.bbox) and p.width > title.bbox[2] - title.bbox[0]:
            if best is None or area(p.bbox) < area(best):
                best = p.bbox
    return best if best is not None else [title.bbox[0] - 2, title.bbox[1] - 2, title.bbox[2] + 2, title.bbox[3] + 2]


def swatch_text(fill, stroke, dashed: bool, n_paths: int) -> str:
    base = colour_name(fill if fill is not None else stroke) or "unknown"
    if dashed:
        base += " dashed"
    if n_paths >= 3:
        base += " hatched"
    return base


def read_legend(objects: list[OverlayObject], lines: list[TextLine], cfg: DiagramsCfg) -> tuple[list[LegendEntry], list[float] | None, TextLine | None]:
    title = next((ln for ln in lines if cfg.legend_title.match(ln.text)), None)
    if title is None:
        return [], None, None
    box = find_box(title, objects)
    inside = [o for o in objects if o.kind == "path" and centre_in(o.bbox, box)
              and o.width < 0.8 * (box[2] - box[0]) and o.index > title.indices[0]]
    entries: list[LegendEntry] = []
    swatch: list[OverlayObject] = []
    labels = sorted((ln for ln in lines if ln is not title and centre_in(ln.bbox, box)), key=lambda ln: ln.indices[0])
    cursor = 0
    for ln in labels:
        swatch = []
        while cursor < len(inside) and inside[cursor].index < ln.indices[0]:
            swatch.append(inside[cursor])
            cursor += 1
        if not swatch:
            continue
        fill = next((p.fill for p in swatch if p.fill is not None), None)
        stroke = next((p.stroke for p in swatch if p.stroke is not None), None)
        dashed = any(p.dashed for p in swatch)
        box_sw = [min(p.bbox[0] for p in swatch), min(p.bbox[1] for p in swatch),
                  max(p.bbox[2] for p in swatch), max(p.bbox[3] for p in swatch)]
        entries.append(LegendEntry(swatch_text(fill, stroke, dashed, len(swatch)), ln.text.strip(), fill, stroke, dashed, box_sw))
    return entries, box, title
