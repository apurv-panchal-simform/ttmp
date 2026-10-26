"""Text lines from drawing order (plan section 2.6: rotated text is reassembled from its glyphs).

The text objects of a drawing are emitted in reading order even where the page matrix is rotated, so a line is a
run of consecutive objects whose origins step steadily in one direction. The step must lie along the object's own
baseline axis (either way round: the sign of the axis is not trusted), so the next label drawn beside a line is
never taken for its continuation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ....config import DiagramsCfg
from ..layers.quality import assess
from .overlay import OverlayObject


@dataclass
class TextLine:
    text: str
    bbox: list[float]
    angle: float                   # reading direction in degrees counter-clockwise, 0 = left to right
    size: float
    indices: list[int] = field(default_factory=list)    # drawing-order indices of its glyph objects
    low_quality: bool = False

    @property
    def key(self) -> str:
        """Upper case without whitespace, the form label patterns are matched on."""
        return "".join(self.text.split()).upper()

    @property
    def centre(self) -> tuple[float, float]:
        return (self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2


def _axis_angle(o: OverlayObject) -> float:
    return math.degrees(math.atan2(o.matrix[1], o.matrix[0])) % 180.0


def _angle_gap(a: float, b: float, period: float = 360.0) -> float:
    d = abs(a - b) % period
    return min(d, period - d)


def build_lines(objects: list[OverlayObject], cfg: DiagramsCfg, quality_threshold: float, min_words: int) -> list[TextLine]:
    """Group the text objects of a page (in drawing order) into lines."""
    lines: list[TextLine] = []
    cur: list[OverlayObject] = []
    direction: float | None = None

    def flush() -> None:
        nonlocal cur, direction
        if cur:
            text = "".join(g.text for g in cur)
            boxes = [g.bbox for g in cur]
            q = assess(text)
            ang = direction if direction is not None else (_axis_angle(cur[0]) if len(cur) else 0.0)
            lines.append(TextLine(
                text, [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
                round(ang % 360.0, 1), max(g.size for g in cur), [g.index for g in cur],
                q.is_low(quality_threshold, min_words) or any(ord(ch) > 0x17f and ch not in "►▶•–—’‘“”" for ch in text)))
        cur, direction = [], None

    prev: OverlayObject | None = None
    for o in objects:
        if o.kind != "text":
            flush()
            prev = None
            continue
        if not o.text:
            continue
        if prev is not None and cur:
            dx, dy = o.origin[0] - prev.origin[0], o.origin[1] - prev.origin[1]
            dist = math.hypot(dx, dy)
            step = math.degrees(math.atan2(dy, dx)) % 360.0
            limit = cfg.line_max_gap_em * max(o.size, prev.size, 1.0)
            ok = 0 < dist <= limit and _angle_gap(step % 180.0, _axis_angle(prev), 180.0) <= cfg.line_turn_deg
            if ok and direction is not None:
                ok = _angle_gap(step, direction) <= cfg.line_turn_deg
            if ok:
                if direction is None:
                    direction = step
                cur.append(o)
                prev = o
                continue
            flush()
        cur.append(o)
        prev = o
    flush()
    return [ln for ln in lines if ln.text.strip()]
