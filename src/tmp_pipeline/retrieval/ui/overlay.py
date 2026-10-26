"""Boxes drawn on a page render: elements by type/route/outcome, record evidence, diagram symbols."""
from __future__ import annotations

import colorsys
import zlib
from collections.abc import Iterable

from PIL import Image, ImageDraw

from ...ingestion.geometry import bbox_pt_to_px


def colour(key: str) -> tuple[int, int, int]:
    """A stable, well-separated colour per category value; categories starting with "!" are red (a problem)."""
    if key.startswith("!"):
        return 220, 20, 20
    h = (zlib.crc32(key.encode()) % 360) / 360
    r, g, b = colorsys.hsv_to_rgb(h, 0.85, 0.85)
    return int(r * 255), int(g * 255), int(b * 255)


def draw_boxes(img: Image.Image, boxes: Iterable[tuple[list[float] | None, str]], highlight: set[int] | None = None,
               width: int = 3, fill_alpha: int = 40) -> Image.Image:
    """``boxes`` are (bbox_px, category); highlighted indices get a thick yellow outline."""
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    highlight = highlight or set()
    for i, (b, cat) in enumerate(boxes):
        if not b or len(b) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in b)
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if i in highlight:
            d.rectangle(box, outline=(255, 215, 0, 255), width=width * 3, fill=(255, 215, 0, 70))
        else:
            c = colour(cat)
            d.rectangle(box, outline=(*c, 255), width=width, fill=(*c, fill_alpha))
    return Image.alpha_composite(base, layer).convert("RGB")


def pt_boxes(bboxes_pt: Iterable[list[float] | None], page_h_pt: float, dpi: float) -> list[list[float] | None]:
    """Point boxes (bottom-left origin) to pixel boxes on a render at ``dpi``."""
    return [bbox_pt_to_px(b, page_h_pt, dpi) if b else None for b in bboxes_pt]


def crop(img: Image.Image, bbox_px: list[float], pad: int = 40) -> Image.Image:
    x0, y0, x1, y1 = bbox_px
    return img.crop((max(0, int(min(x0, x1)) - pad), max(0, int(min(y0, y1)) - pad),
                     min(img.width, int(max(x0, x1)) + pad), min(img.height, int(max(y0, y1)) + pad)))
