"""PDF layer reader (plan section 1.2).

pdfplumber supplies characters (font, size, colour), rects, lines, curves and images; pypdf supplies
AcroForm fields; pypdfium2 supplies page objects in drawing order, with nested form-XObject children
mapped into page space. Everything is converted once, here, to PDF points with origin bottom-left.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pypdf
import pypdfium2 as pdfium
import pypdfium2.raw as raw

_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demi")
_KIND = {
    raw.FPDF_PAGEOBJ_TEXT: "text", raw.FPDF_PAGEOBJ_PATH: "path", raw.FPDF_PAGEOBJ_IMAGE: "image",
    raw.FPDF_PAGEOBJ_SHADING: "shading", raw.FPDF_PAGEOBJ_FORM: "form",
}


@dataclass
class Char:
    text: str
    font: str
    size: float
    bold: bool
    colour: tuple
    x0: float
    y0: float
    x1: float
    y1: float
    rotation: int          # degrees counter-clockwise: 0, 90, 180, 270 (or raw angle if oblique)


@dataclass
class Shape:
    kind: str              # rect | line | curve
    x0: float
    y0: float
    x1: float
    y1: float
    fill: bool
    stroke: bool
    fill_colour: tuple | None
    stroke_colour: tuple | None
    linewidth: float = 0.0


@dataclass
class ImageObj:
    x0: float
    y0: float
    x1: float
    y1: float
    width_px: int
    height_px: int


@dataclass
class PageObject:
    index: int             # drawing order within the page
    kind: str              # text | path | image | shading | form
    level: int             # 0 = page content, 1+ = inside a form XObject
    bbox_pt: list[float]   # page space, bottom-left origin
    parent: int | None = None


@dataclass
class FormField:
    name: str
    kind: str              # checkbox | radio | text | choice | button | other
    value: object
    checked: bool | None
    bbox_pt: list[float]


@dataclass
class PageLayers:
    page: int
    width: float
    height: float
    chars: list[Char] = field(default_factory=list)
    rects: list[Shape] = field(default_factory=list)
    lines: list[Shape] = field(default_factory=list)
    curves: list[Shape] = field(default_factory=list)
    images: list[ImageObj] = field(default_factory=list)
    fields: list[FormField] = field(default_factory=list)
    objects: list[PageObject] = field(default_factory=list)


def _colour(c):
    if c is None:
        return None
    if isinstance(c, (int, float)):
        return (float(c),) * 3
    try:
        c = tuple(float(v) for v in c)
    except (TypeError, ValueError):   # pattern / separation colour names: no usable RGB
        return None
    if len(c) == 1:
        return c * 3
    if len(c) == 4:      # CMYK -> RGB
        cc, m, y, k = c
        return ((1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return c


def _rotation(matrix) -> int:
    a, b = matrix[0], matrix[1]
    ang = math.degrees(math.atan2(b, a)) % 360
    snapped = round(ang / 90) * 90 % 360
    return int(snapped) if abs(ang - round(ang / 90) * 90) < 5 else int(round(ang))


def _box(d, h):
    return d["x0"], h - d["bottom"], d["x1"], h - d["top"]


def _shape(kind, d, h) -> Shape:
    x0, y0, x1, y1 = _box(d, h)
    return Shape(kind, x0, y0, x1, y1, bool(d.get("fill")), bool(d.get("stroke")),
                 _colour(d.get("non_stroking_color")) if d.get("fill") else None,
                 _colour(d.get("stroking_color")) if d.get("stroke") else None,
                 float(d.get("linewidth") or 0))


def apply_matrix(m, box):
    """Transform [x0, y0, x1, y1] by matrix (a, b, c, d, e, f); return the enclosing box."""
    a, b, c, d, e, f = m
    xs, ys = [], []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return [min(xs), min(ys), max(xs), max(ys)]


def compose_matrix(inner, outer):
    """Matrix applying `inner` first, then `outer`."""
    a1, b1, c1, d1, e1, f1 = inner
    a2, b2, c2, d2, e2, f2 = outer
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _page_objects(pdfium_page) -> list[PageObject]:
    out: list[PageObject] = []
    cum: dict[int, tuple] = {}            # level -> matrix mapping that level's children into page space
    last_form_index: dict[int, int] = {}
    for i, obj in enumerate(pdfium_page.get_objects(max_depth=4)):
        lvl = obj.level
        bounds = list(obj.get_bounds())
        kind = _KIND.get(obj.type, "other")
        if lvl > 0:
            bounds = apply_matrix(cum[lvl - 1], bounds) if (lvl - 1) in cum else bounds
        out.append(PageObject(i, kind, lvl, [round(v, 2) for v in bounds],
                              last_form_index.get(lvl - 1) if lvl > 0 else None))
        if kind == "form":
            m = tuple(obj.get_matrix().get())
            cum[lvl] = compose_matrix(m, cum[lvl - 1]) if lvl > 0 and (lvl - 1) in cum else m
            last_form_index[lvl] = i
    return out


def _form_fields(reader: pypdf.PdfReader, page_index: int) -> list[FormField]:
    fields: list[FormField] = []
    annots = reader.pages[page_index].get("/Annots") or []
    for ref in annots:
        a = ref.get_object()
        if a.get("/Subtype") != "/Widget":
            continue
        parent = a.get("/Parent")
        parent = parent.get_object() if parent is not None else {}
        name = str(a.get("/T") or parent.get("/T") or "")
        ft = str(a.get("/FT") or parent.get("/FT") or "")
        flags = int(a.get("/Ff") or parent.get("/Ff") or 0)
        value = a.get("/V") if a.get("/V") is not None else parent.get("/V")
        state = a.get("/AS")
        rect = [float(v) for v in a["/Rect"]]
        rect = [min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3])]
        if ft == "/Btn":
            kind = "button" if flags & (1 << 16) else ("radio" if flags & (1 << 15) else "checkbox")
            checked = (state not in (None, "/Off")) if state is not None else (str(value) not in ("None", "/Off"))
        else:
            kind = {"/Tx": "text", "/Ch": "choice"}.get(ft, "other")
            checked = None
        fields.append(FormField(name, kind, None if value is None else str(value), checked, rect))
    return fields


def read_layers(pdf_path: str | Path) -> list[PageLayers]:
    path = str(pdf_path)
    reader = pypdf.PdfReader(path)
    doc = pdfium.PdfDocument(path)
    result: list[PageLayers] = []
    try:
        with pdfplumber.open(path) as plumber:
            for i, page in enumerate(plumber.pages):
                h = float(page.height)
                layer = PageLayers(page=i + 1, width=float(page.width), height=h)
                for ch in page.chars:
                    x0, y0, x1, y1 = _box(ch, h)
                    font = ch.get("fontname", "")
                    layer.chars.append(Char(
                        ch["text"], font, float(ch.get("size", 0)),
                        any(k in font.lower() for k in _BOLD_HINTS),
                        _colour(ch.get("non_stroking_color")) or (0.0, 0.0, 0.0),
                        x0, y0, x1, y1, _rotation(ch.get("matrix", (1, 0, 0, 1, 0, 0)))))
                layer.rects = [_shape("rect", r, h) for r in page.rects]
                layer.lines = [_shape("line", l, h) for l in page.lines]
                layer.curves = [_shape("curve", c, h) for c in page.curves]
                for im in page.images:
                    x0, y0, x1, y1 = _box(im, h)
                    sw = im.get("srcsize") or (0, 0)
                    layer.images.append(ImageObj(x0, y0, x1, y1, int(sw[0]), int(sw[1])))
                layer.fields = _form_fields(reader, i)
                layer.objects = _page_objects(doc[i])
                result.append(layer)
    finally:
        doc.close()
    return result
