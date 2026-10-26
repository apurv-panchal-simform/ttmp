"""The drawing as the PDF stores it (plan section 2.6, vector reading).

A second pypdfium2 pass over one page, kept apart from the Phase 1 layers so ordinary pages pay nothing for it.
Objects come in drawing order with form-XObject transforms composed, so every box and origin is in page
space (points, origin bottom-left). Order matters: a sign is drawn as a plate, then its glyphs, and
consecutive text objects are drawn in reading order even where the page matrix is rotated or mirrored.
"""
from __future__ import annotations

import ctypes
import math
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..layers.reader import apply_matrix, compose_matrix
from ..layers.render import perceptual_hash

_KIND = {
    raw.FPDF_PAGEOBJ_TEXT: "text", raw.FPDF_PAGEOBJ_PATH: "path", raw.FPDF_PAGEOBJ_IMAGE: "image",
    raw.FPDF_PAGEOBJ_SHADING: "shading", raw.FPDF_PAGEOBJ_FORM: "form",
}
Colour = tuple[int, int, int]


@dataclass
class OverlayObject:
    index: int                      # drawing order within the page
    kind: str                       # text | path | image | shading | form
    level: int                      # 0 = page content, 1+ = inside a form XObject
    bbox: list[float]               # page space
    fill: Colour | None = None
    stroke: Colour | None = None
    stroke_width: float = 0.0
    segments: int = 0               # path segment count
    dashed: bool = False
    text: str = ""
    size: float = 0.0               # text size in page space (from the composed matrix)
    origin: tuple[float, float] = (0.0, 0.0)   # text origin / image placement origin, page space
    matrix: tuple[float, ...] = (1, 0, 0, 1, 0, 0)   # composed into page space
    px: tuple[int, int] = (0, 0)    # image source size
    phash: str | None = None        # image: 64-bit hash of the source pixels

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def centre(self) -> tuple[float, float]:
        return (self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class OverlayPage:
    page: int
    width: float
    height: float
    objects: list[OverlayObject] = field(default_factory=list)

    def of_kind(self, *kinds: str) -> list[OverlayObject]:
        return [o for o in self.objects if o.kind in kinds]


def _rgb(obj, fn) -> Colour | None:
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not fn(obj.raw, r, g, b, a) or a.value == 0:
        return None
    return r.value, g.value, b.value


def _path_style(obj) -> tuple[Colour | None, Colour | None, float, int, bool]:
    fill_mode, stroke = ctypes.c_int(), ctypes.c_int()
    raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill_mode), ctypes.byref(stroke))
    width = ctypes.c_float()
    raw.FPDFPageObj_GetStrokeWidth(obj.raw, ctypes.byref(width))
    fill = _rgb(obj, raw.FPDFPageObj_GetFillColor) if fill_mode.value else None
    edge = _rgb(obj, raw.FPDFPageObj_GetStrokeColor) if stroke.value else None
    dashed = bool(raw.FPDFPageObj_GetDashCount(obj.raw) > 0)
    return fill, edge, round(width.value, 3), int(raw.FPDFPath_CountSegments(obj.raw)), dashed


def _image_info(obj) -> tuple[tuple[int, int], str | None]:
    try:
        px = tuple(int(v) for v in obj.get_px_size())
    except Exception:
        px = (0, 0)
    try:
        return (px[0], px[1]), perceptual_hash(obj.get_bitmap(render=False).to_pil())
    except Exception:                      # an undecodable image has no hash and is never treated as a duplicate
        return (px[0], px[1]), None


def _point(m, x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def read_overlay(page_obj, page_no: int, *, hash_images: bool = True) -> OverlayPage:
    """Read one pypdfium2 page. ``page_obj`` stays owned by the caller."""
    w, h = page_obj.get_size()
    out = OverlayPage(page_no, float(w), float(h))
    tp = page_obj.get_textpage()
    cum: dict[int, tuple] = {}
    try:
        for i, o in enumerate(page_obj.get_objects(max_depth=4, textpage=tp)):
            lvl, kind = o.level, _KIND.get(o.type, "other")
            local = tuple(float(v) for v in o.get_matrix().get())
            parent = cum.get(lvl - 1) if lvl > 0 else None
            m = compose_matrix(local, parent) if parent else local
            bbox = [float(v) for v in o.get_bounds()]
            if parent:
                bbox = apply_matrix(parent, bbox)
            obj = OverlayObject(i, kind, lvl, [round(v, 2) for v in bbox], matrix=m)
            if kind == "form":
                cum[lvl] = m
            elif kind == "path":
                obj.fill, obj.stroke, obj.stroke_width, obj.segments, obj.dashed = _path_style(o)
            elif kind == "text":
                try:
                    obj.text = o.extract()
                except Exception:
                    obj.text = ""
                font = o.get_font_size()
                obj.size = round((font if font > 0 else 1.0) * math.hypot(m[2], m[3]), 2)
                obj.origin = _point(parent, local[4], local[5]) if parent else (local[4], local[5])
            elif kind == "image":
                obj.origin = _point(m, 0.0, 0.0)
                if hash_images:
                    obj.px, obj.phash = _image_info(o)
                else:
                    obj.px = _image_info(o)[0]
            out.objects.append(obj)
    finally:
        tp.close()
    return out


def read_overlays(pdf_path: str | Path, pages: list[int], *, hash_images: bool = True) -> dict[int, OverlayPage]:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return {p: read_overlay(doc[p - 1], p, hash_images=hash_images) for p in pages}
    finally:
        doc.close()
