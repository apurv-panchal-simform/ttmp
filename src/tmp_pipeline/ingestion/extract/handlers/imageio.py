"""Pictures for image prompts: crops of the master render, and the words a model may cite (plan sections 2.6, 2.7).

Citable words are DI words (``ocr_word`` elements, ids kept from DI) when Document Intelligence has run, otherwise the
lines of the PDF text layer inside the crop; either way every id exists in the store, so a cited id can be checked.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from ...contracts import HandlerContext
from ...geometry import bbox_pt_to_px, centre_in
from ...validators import Word
from ...vocab import ElementType


def master_path(ctx: HandlerContext, page: int) -> Path | None:
    info = ctx.store.pages.get(page)
    if ctx.out_dir is None or info is None or not info.master:
        return None
    p = ctx.out_dir / info.master["file"]
    return p if p.exists() else None


def open_image(path: Path | None) -> Image.Image | None:
    if path is None or not path.exists():
        return None
    img = Image.open(path)
    img.load()
    return img.convert("RGB")


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def crop_master(master: Image.Image, bbox_pt, page_h_pt: float, dpi: float = 200.0, pad_pt: float = 0.0) -> Image.Image:
    x0, y0, x1, y1 = bbox_pt
    u0, v0, u1, v1 = bbox_pt_to_px([x0 - pad_pt, y0 - pad_pt, x1 + pad_pt, y1 + pad_pt], page_h_pt, dpi)
    w, h = master.size
    return master.crop((max(0, round(u0)), max(0, round(v0)), min(w, round(u1)), min(h, round(v1))))


def words_in(ctx: HandlerContext, page: int, bbox_pt) -> list[Word]:
    """The words a model may cite inside ``bbox_pt``: DI words if any exist on the page, else PDF text lines."""
    di = [e for e in ctx.store.of_type(ElementType.OCR_WORD, page=page) if centre_in(e.bbox_pt, bbox_pt)]
    if di:
        return [Word(e.element_id, e.representations.get("text", ""), list(e.bbox_pt)) for e in di]
    out: list[Word] = []
    for e in ctx.store.of_type(ElementType.TEXT_BLOCK, page=page):
        for n, ln in enumerate(e.representations.get("lines", [])):
            if ln["text"].strip() and centre_in(ln["bbox"], bbox_pt):
                out.append(Word(f"{e.element_id}:l{n}", ln["text"].strip(), list(ln["bbox"])))
    return out


def nearby_text(ctx: HandlerContext, page: int, bbox_pt, reach_pt: float = 40.0) -> str:
    """Text of blocks that touch the area just around the picture (its caption or lead-in)."""
    zone = [bbox_pt[0] - reach_pt, bbox_pt[1] - reach_pt, bbox_pt[2] + reach_pt, bbox_pt[3] + reach_pt]
    texts = []
    for e in ctx.store.of_type(ElementType.TEXT_BLOCK, page=page):
        if centre_in(e.bbox_pt, zone) and not centre_in(e.bbox_pt, bbox_pt):
            texts.append(e.representations.get("text", "").replace("\n", " ").strip())
    return " ".join(t for t in texts if t)[:300]
