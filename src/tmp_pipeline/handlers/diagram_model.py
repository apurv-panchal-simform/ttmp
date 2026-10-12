"""The model-assisted steps of a diagram sheet (plan section 2.6): X10 captions, X8 summary, X7 figures, X9 fallback.

Each step asks through ``ask`` (None = no model, so nothing is invented), validates with the code checks in
``model.validators``, and returns plain data for the sheet record. A claim that fails a check is kept, marked
``unverified`` with its problems, never edited.
"""
from __future__ import annotations

from ..contracts import HandlerContext
from ..diagrams.models import SheetModel
from ..geometry import bbox_pt_to_px
from ..model import prompts
from ..model.client import ask
from ..model.validators import agree_x9, check_x8, check_x9, check_x10, x9_key
from ..models import Element
from ..vocab import SymbolKind
from .imageio import crop_master, master_path, open_image, png_bytes, words_in
from .imageread import read_image

_LABEL_ONLY = (SymbolKind.LINE, SymbolKind.CONES, SymbolKind.AREA)


def legend_text(sheet: SheetModel) -> str:
    return "; ".join(f"{e.swatch} = {e.meaning}" for e in sheet.legend) or "(no legend)"


def _px(ctx: HandlerContext, element: Element, bbox) -> list[int]:
    return [round(v) for v in bbox_pt_to_px(bbox, element.page_size_pt[1], 200)]


def symbol_list(ctx: HandlerContext, element: Element, sheet: SheetModel) -> str:
    rows = []
    for s in sheet.symbols:
        if s.kind in _LABEL_ONLY:
            continue
        rows.append(f"{s.id} | {s.kind} | {s.text or s.value or ''} | RP {s.rp if s.rp is not None else '-'} | "
                    f"{s.road or '-'} | {_px(ctx, element, s.bbox)}")
    return "\n".join(rows)


def caption_symbols(ctx: HandlerContext, element: Element, sheet: SheetModel) -> dict[str, dict]:
    """X10 for every symbol marked ``needs_caption``: {symbol id: {caption, status, notes}}."""
    out: dict[str, dict] = {}
    todo = [s for s in sheet.symbols if s.needs_caption]
    master = open_image(master_path(ctx, element.page)) if todo else None
    if master is None:
        return out
    for s in todo:
        crop = png_bytes(crop_master(master, s.bbox, element.page_size_pt[1], pad_pt=4))
        colours = ", ".join(f"rgb{c}" for c in s.colours) or "none"
        result = ask(ctx.model, prompts.x10_caption_symbol(crop, legend_text(sheet), colours))
        caption, problems = check_x10(result, s.text)
        if result is None:
            continue
        out[s.id] = {"caption": caption, "confidence": (result or {}).get("confidence"),
                     "status": "verified" if caption and not problems else "unverified", "notes": problems}
    return out


def summarise(ctx: HandlerContext, element: Element, sheet: SheetModel, captions: dict[str, dict]) -> dict | None:
    """X8: one spatial summary per sheet, checked claim by claim against the extracted symbols."""
    overlay, master = (open_image(ctx.out_dir / p) if (ctx.out_dir and p) else None for p in (sheet.overlay, sheet.master))
    if overlay is None or master is None:
        return None
    result = ask(ctx.model, prompts.x8_sheet_summary(
        png_bytes(overlay), png_bytes(master), sheet.sheet or "", sheet.title or "", legend_text(sheet),
        symbol_list(ctx, element, sheet), "; ".join(sheet.notes) or "(none)"))
    if result is None:
        return None
    symbols = {s.id: {"text": s.text, "value": s.value, "rp": s.rp} for s in sheet.symbols}
    extra = " ".join([*sheet.notes, *sheet.callouts, *sheet.title_block.values(),
                      *(c.get("caption") or "" for c in captions.values())])
    chk = check_x8(result, symbols, extra)
    return {"approaches": result.get("approaches") or [], "layout": result.get("layout") or [],
            "pedestrians": result.get("pedestrians") or [], "unclear": result.get("unclear") or [],
            "verified": chk.verified, "claims": chk.claims, "problems": chk.problems,
            "model": prompts.MODELS["X8"]}


def read_figures(ctx: HandlerContext, element: Element, sheet: SheetModel) -> list[dict]:
    """X7 for the pictures drawn beside the plan (pedestrian options, notices), once per distinct picture."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    by_box = {tuple(round(v) for v in i.bbox): i for i in sheet.images if i.role == "figure"}
    for r in (r for r in sheet.regions if r.kind == "figure"):
        pic = next((i for k, i in by_box.items() if all(abs(a - b) <= 40 for a, b in zip(k, r.bbox, strict=True))), None)
        key = pic.phash if pic is not None and pic.phash else None
        if key and key in seen:
            out.append({**seen[key], "region": r.bbox, "reused": True})
            continue
        reading = read_image(ctx, element, r.bbox, heading=sheet.title or "", title=r.label or "")
        item = {"region": r.bbox, "title": r.label, "status": reading.status, "notes": reading.notes, "reused": False,
                "kind": (reading.result or {}).get("kind"), "description": (reading.result or {}).get("description"),
                "items": reading.items, "verified": reading.all_verified and bool(reading.check and reading.check.ok)}
        if key:
            seen[key] = item
        out.append(item)
    return out


def _tiles(bbox, cols: int, rows: int, overlap: float) -> list[list[float]]:
    x0, y0, x1, y1 = bbox
    w, h = (x1 - x0) / cols, (y1 - y0) / rows
    return [[max(x0, x0 + c * w - overlap), max(y0, y0 + r * h - overlap), min(x1, x0 + (c + 1) * w + overlap),
             min(y1, y0 + (r + 1) * h + overlap)] for r in range(rows) for c in range(cols)]


def read_flattened(ctx: HandlerContext, element: Element, sheet: SheetModel) -> dict:
    """X9 on tiles of a sheet with no vector overlay, with a second read on a tighter crop that sees no word list."""
    master = open_image(master_path(ctx, element.page))
    d = ctx.cfg.diagrams
    out: dict = {"tiles": [], "reads": 0}
    if master is None:
        out["notes"] = ["no master render to crop from"]
        return out
    size = element.page_size_pt
    plan = next((r.bbox for r in sheet.regions if r.kind == "plan"), [14.0, 96.0, size[0] - 14.0, size[1] - 10.0])
    for box in _tiles(plan, d.tile_cols, d.tile_rows, d.tile_overlap_pt):
        words = words_in(ctx, element.page, box)
        wd = {w.id: w for w in words}
        crop = png_bytes(crop_master(master, box, size[1]))
        first = ask(ctx.model, prompts.x9_read_region(crop, "plan tile", legend_text(sheet), [(w.id, w.text) for w in words]))
        if first is None:
            out["tiles"].append({"bbox": box, "status": "skipped", "items": []})
            continue
        out["reads"] += 1
        chk = check_x9(first, wd)
        tight = [box[0] + 0.1 * (box[2] - box[0]), box[1] + 0.1 * (box[3] - box[1]),
                 box[2] - 0.1 * (box[2] - box[0]), box[3] - 0.1 * (box[3] - box[1])]
        second = ask(ctx.model, prompts.x9_read_region(png_bytes(crop_master(master, tight, size[1])), "plan tile",
                                                       legend_text(sheet), None, second_read=True))
        agreed = agree_x9(first, second)
        items = []
        for it in chk.items:
            both = x9_key(it.what, it.value) in agreed
            items.append({"what": it.what, "value": it.value, "grounded": it.ok, "problems": it.problems,
                          "status": "verified" if it.ok and both else "unverified"})
        out["tiles"].append({"bbox": box, "status": "read", "items": items, "first": first, "second": second,
                             "model": [prompts.MODELS["X9"], prompts.READ2_MODEL]})
    return out
