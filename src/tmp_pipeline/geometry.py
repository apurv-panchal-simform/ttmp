"""Box and coordinate helpers shared by every stage.

Boxes are ``[x0, y0, x1, y1]`` in PDF points with the origin at the bottom-left (so y0 <= y1), except the
pixel helpers, which return top-left-origin pixel boxes on a master render.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .constants import MASTER_DPI

Rect = Sequence[float]
Point = tuple[float, float]


def centre(b: Rect) -> Point:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def area(b: Rect) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def contains_point(box: Rect, p: Point) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def centre_in(inner: Rect, outer: Rect) -> bool:
    """True when the centre of ``inner`` lies inside ``outer`` (the plan's rule for "this text belongs to that box")."""
    return contains_point(outer, centre(inner))


def contains(outer: Rect, inner: Rect, pad: float = 0.0) -> bool:
    """True when ``inner`` lies wholly inside ``outer``, allowing ``pad`` points of slack on every side."""
    return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
            and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def intersection(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def iou(a: Rect, b: Rect) -> float:
    inter = intersection(a, b)
    union_area = area(a) + area(b) - inter
    return inter / union_area if union_area else 0.0


def union(boxes: Iterable[Rect]) -> list[float]:
    """Smallest box that holds every box given; raises ValueError for none."""
    bs = list(boxes)
    if not bs:
        raise ValueError("union of no boxes")
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def smallest_containing(inner: Rect, candidates: Iterable[Rect]) -> Rect | None:
    """The smallest candidate whose box holds the centre of ``inner``, or None."""
    best: Rect | None = None
    for c in candidates:
        if centre_in(inner, c) and (best is None or area(c) < area(best)):
            best = c
    return best


def in_band(b: Rect, page_height_pt: float, header_pt: float, footer_pt: float) -> str | None:
    """"header" or "footer" when the vertical centre of ``b`` sits in that band of the page, else None."""
    cy = centre(b)[1]
    if cy > page_height_pt - header_pt:
        return "header"
    if cy < footer_pt:
        return "footer"
    return None


# --- points <-> master-render pixels -------------------------------------------------------------------------

def pt_to_px(x_pt: float, y_pt: float, page_h_pt: float, dpi: float) -> tuple[float, float]:
    s = dpi / 72
    return x_pt * s, (page_h_pt - y_pt) * s


def px_to_pt(u: float, v: float, page_h_pt: float, dpi: float) -> tuple[float, float]:
    s = dpi / 72
    return u / s, page_h_pt - v / s


def bbox_pt_to_px(b: Rect, page_h_pt: float, dpi: float) -> list[float]:
    """[x0, y0, x1, y1] in points (bottom-left origin) -> [u0, v0, u1, v1] pixels (top-left origin)."""
    u0, v1 = pt_to_px(b[0], b[1], page_h_pt, dpi)
    u1, v0 = pt_to_px(b[2], b[3], page_h_pt, dpi)
    return [u0, v0, u1, v1]


def bbox_px_to_pt(b: Rect, page_h_pt: float, dpi: float) -> list[float]:
    x0, y1 = px_to_pt(b[0], b[1], page_h_pt, dpi)
    x1, y0 = px_to_pt(b[2], b[3], page_h_pt, dpi)
    return [x0, y0, x1, y1]


def rounded_px_box(b: Rect, page_h_pt: float, dpi: float = MASTER_DPI) -> list[float]:
    """Pixel box on the master render, rounded to 0.1 px for storage."""
    return [round(v, 1) for v in bbox_pt_to_px(b, page_h_pt, dpi)]
