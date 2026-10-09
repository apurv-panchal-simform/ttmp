"""DI reports PDF pages in inches from the top-left; elements use points from the bottom-left."""
from __future__ import annotations

PT_PER_INCH = 72.0
SIZE_TOL_PT = 1.0
ANGLE_TOL_DEG = 1.0


def polygon_to_bbox_pt(polygon: list[float], page_h_pt: float) -> list[float]:
    """[x1, y1, x2, y2, ...] inches (top-left origin) -> [x0, y0, x1, y1] points (bottom-left origin)."""
    xs, ys = polygon[0::2], polygon[1::2]
    return [min(xs) * PT_PER_INCH, page_h_pt - max(ys) * PT_PER_INCH,
            max(xs) * PT_PER_INCH, page_h_pt - min(ys) * PT_PER_INCH]


def check_page_geometry(di_page: dict, pdf_w_pt: float, pdf_h_pt: float) -> list[str]:
    """Problems that make DI coordinates unsafe to convert; an empty list means they are fine."""
    problems = []
    if di_page.get("unit") != "inch":
        problems.append(f"unit is {di_page.get('unit')!r}, expected 'inch'")
        return problems
    w, h = di_page["width"] * PT_PER_INCH, di_page["height"] * PT_PER_INCH
    if abs(w - pdf_w_pt) > SIZE_TOL_PT or abs(h - pdf_h_pt) > SIZE_TOL_PT:
        problems.append(f"page size {w:.1f}x{h:.1f} pt differs from the PDF's {pdf_w_pt:.1f}x{pdf_h_pt:.1f} pt")
    angle = di_page.get("angle", 0) or 0
    if min(abs(angle) % 360, 360 - abs(angle) % 360) > ANGLE_TOL_DEG:
        problems.append(f"page angle {angle} is not about 0")
    return problems
