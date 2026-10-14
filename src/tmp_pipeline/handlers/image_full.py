"""X13: read an image completely, so questions about it can be answered from text later (plan 2.6, 2.7 extended).

A diagram sheet is read whole (vector overlay and master render together) and, with ``[diagrams.describe] read_tiles``,
again in tiles of the plan so small detail is legible. Each read is given the code facts (``diagrams.describe``) with
their IDs and must use them; ``check_x13`` checks every statement (IDs exist, numbers are on the sheet, broken-font
readings keep the printed letters). Tile reads add item detail; the whole read gives the description and purpose.
Pictures (beside a plan, or in the forms) get one read with the words of the text layer inside them as their items.
"""
from __future__ import annotations

from dataclasses import asdict

from ..contracts import HandlerContext
from ..diagrams.models import SheetModel
from ..geometry import centre_in
from ..model import prompts
from ..model.client import ask
from ..model.validators import ImageReadCheck, _numbers, check_x13
from ..models import Element
from .imageio import crop_master, master_path, nearby_text, open_image, png_bytes, words_in


def _allowed(*texts: object) -> set[float]:
    out: set[float] = set()
    for t in texts:
        out |= _numbers(str(t or ""))
    return out


def _merge(reads: list[ImageReadCheck]) -> dict:
    """One reading from several (whole first, then tiles): a checked statement wins over an unchecked one."""
    whole = reads[0]
    items: dict[str, dict] = {}
    new: list[dict] = []
    for r in reads:
        for it in r.items:
            if it["item_id"] == "new":
                if not any(n.get("looks_like") == it.get("looks_like") for n in new):
                    new.append(it)
            elif it["item_id"] not in items or (it["ok"] and not items[it["item_id"]]["ok"]):
                items[it["item_id"]] = it
    dists: dict[str, dict] = {}
    for r in reads:
        for d in r.distances:
            if str(d["item_id"]) not in dists or (d["ok"] and not dists[str(d["item_id"])]["ok"]):
                dists[str(d["item_id"])] = d
    rels, seen = [], set()
    for r in reads:
        for x in r.relations:
            key = (x.get("a"), x.get("relation"), x.get("b"))
            if key not in seen:
                seen.add(key)
                rels.append(x)
    garbled: dict[str, dict] = {}
    for r in reads:
        for g in r.garbled:
            if g["id"] not in garbled or g["match"] > garbled[g["id"]]["match"]:
                garbled[g["id"]] = g
    unread = [u for n, u in enumerate(whole.unreadable) if u not in whole.unreadable[:n]]      # a tile's edge cuts things
    inferred = list(dict.fromkeys(whole.inferred))                                           # off: whole read only
    return {"texts": whole.texts, "items": list(items.values()) + new, "distances": list(dists.values()),
            "relations": rels, "garbled": list(garbled.values()), "unreadable": unread, "inferred": inferred,
            "problems": [p for r in reads for p in r.problems]}


def _tiles(bbox: list[float], cols: int, rows: int, overlap: float) -> list[list[float]]:
    x0, y0, x1, y1 = bbox
    w, h = (x1 - x0) / cols, (y1 - y0) / rows
    return [[max(x0, x0 + c * w - overlap), max(y0, y0 + r * h - overlap), min(x1, x0 + (c + 1) * w + overlap),
             min(y1, y0 + (r + 1) * h + overlap)] for r in range(rows) for c in range(cols)]


def read_sheet(ctx: HandlerContext, element: Element, sheet: SheetModel, facts: dict, legend: str) -> dict | None:
    """X13 on one diagram sheet: the whole sheet, then its tiles. None without a model or a render."""
    if ctx.model is None or not ctx.out_dir:
        return None
    overlay = open_image(ctx.out_dir / sheet.overlay) if sheet.overlay else None
    master = open_image(ctx.out_dir / sheet.master) if sheet.master else None
    if master is None:
        return None
    cfg = ctx.cfg.diagrams
    items = facts["items"]
    dists = facts["distances"]
    garbled = {f"g{n}": t["text"] for n, t in enumerate((t for t in facts["texts"] if t["garbled"]), 1)}
    allowed = _allowed(*(t["text"] for t in facts["texts"]), *(i.get("rp") for i in items),
                       *((i.get("extra") or {}).get("value") for i in items), *(d["value"] for d in dists))
    title = f"{sheet.sheet or ''} {sheet.title or ''}".strip()
    notes = "; ".join(sheet.notes) or "(none)"

    def rows(box: list[float] | None) -> tuple[str, str, str]:
        its = [i for i in items if box is None or centre_in(i["bbox"], box)]
        ids = {i["id"] for i in its}
        it_txt = "\n".join(f"{i['id']} | {i['label']} | {i['location']}" for i in its)
        d_txt = "\n".join(f"{d['id']} | {d['value']} {d['unit']} | from {d['start'] or 'not found'} to {d['end'] or 'not found'}"
                          for d in dists if box is None or d["id"] in ids)
        g_txt = "\n".join(f"{k} | {v}" for k, v in garbled.items()) if box is None else ""
        return it_txt, d_txt, g_txt

    item_ids = {i["id"] for i in items}
    dist_ids = {d["id"] for d in dists}
    reads: list[ImageReadCheck] = []
    it_txt, d_txt, g_txt = rows(None)
    images = (png_bytes(overlay), png_bytes(master)) if overlay is not None else (png_bytes(master),)
    names = ("image 1: the drawing without the photo; image 2: the drawing over the aerial photo"
             if overlay is not None else "image: the sheet")
    result = ask(ctx.model, prompts.x13_read_image(images, names, "a whole temporary traffic management diagram sheet",
                                                   title, legend, notes, it_txt, d_txt, g_txt))
    if result is None:
        return None
    reads.append(check_x13(result, item_ids, dist_ids, allowed, garbled, cfg.describe.garbled_match_min))
    if cfg.describe.read_tiles:
        page_h = element.page_size_pt[1]
        for box in _tiles(facts["frame"], cfg.tile_cols, cfg.tile_rows, cfg.tile_overlap_pt):
            it_txt, d_txt, _ = rows(box)
            if not it_txt:
                continue
            res = ask(ctx.model, prompts.x13_read_image((png_bytes(crop_master(master, box, page_h)),), "image: one part of the sheet",
                                                        "one part (tile) of a diagram sheet, enlarged", title, legend, notes,
                                                        it_txt, d_txt, "", whole=False))
            if res is not None:
                reads.append(check_x13(res, item_ids, dist_ids, allowed, garbled, cfg.describe.garbled_match_min))
    return {**_merge(reads), "reads": len(reads), "model": prompts.MODELS["X13"]}


def read_picture(ctx: HandlerContext, element: Element, bbox_pt, *, title: str = "", scope: str = "a picture") -> dict | None:
    """X13 on one picture (beside a plan or in the forms). Its items are the text-layer words inside it."""
    if ctx.model is None:
        return None
    master = open_image(master_path(ctx, element.page))
    if master is None:
        return None
    words = words_in(ctx, element.page, bbox_pt)
    items = {w.id: w.text for w in words}
    near = nearby_text(ctx, element.page, bbox_pt)
    res = ask(ctx.model, prompts.x13_read_image((png_bytes(crop_master(master, bbox_pt, element.page_size_pt[1])),),
                                                "image: the picture", scope, title or "(no title)", "(none)",
                                                f"text near the picture: {near}" if near else "(none)",
                                                "\n".join(f"{k} | text \"{v}\" | inside the picture" for k, v in items.items()),
                                                "", ""))
    if res is None:
        return None
    chk = check_x13(res, set(items), set(), _allowed(*items.values(), near, title), {}, 1.0)
    return {**asdict(chk), "reads": 1, "model": prompts.MODELS["X13"]}
