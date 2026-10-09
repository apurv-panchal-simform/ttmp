"""Layer data -> coarse elements (plan sections 1.4 and 1.6).

Elements start in zone "unclassified"; zones.py assigns zones per region afterwards. Text inside a table
becomes a child of that table (claimed by it), so nothing is stored twice.
"""
from __future__ import annotations

from ..logging_ import log_event
from ..models import ElementStore, bbox_pt_to_px
from .gridfind import detect_tables
from .quality import assess
from .reader import PageLayers
from .textblocks import build_text_blocks
from .ticks import detect_ticks

SOURCE = "pdf-layers"
MASTER_DPI = 200


def _centre_in(bbox, outer) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _px(bbox, h):
    return [round(v, 1) for v in bbox_pt_to_px(bbox, h, MASTER_DPI)]


def build_elements(layers: list[PageLayers], doc_id: str, cfg: dict) -> ElementStore:
    store = ElementStore(doc_id)
    low, min_words = cfg["quality"]["low_threshold"], cfg["quality"]["min_words"]
    for page in layers:
        size = (page.width, page.height)
        h = page.height
        grids = detect_tables(page, cfg)
        table_ids: list[str] = []
        for g in grids:
            el = store.add(page.page, size, "unclassified", "table", g.bbox, bbox_px=_px(g.bbox, h), source=SOURCE,
                           metadata={"n_xs": g.n_xs, "n_ys": g.n_ys, "n_parts": g.n_parts,
                                     "n_fills": g.n_fills, "rating_cells": g.rating_cells})
            table_ids.append(el.element_id)
        blocks = build_text_blocks(page, grids, cfg)
        qualities = []
        for b in blocks:
            qa = assess(b.text)
            q = qa.score
            flags = ["low_text_quality"] if qa.is_low(low, min_words) else []
            if q is not None:
                qualities.append(q)
            store.add(page.page, size, "unclassified", "text_block", b.bbox, bbox_px=_px(b.bbox, h), flags=flags,
                      parent_id=table_ids[b.table] if b.table is not None else None, source=SOURCE,
                      representations={"text": b.text},
                      metadata={"rotation": b.rotation, "band": b.band, "size": round(b.size, 1), "bold": b.bold,
                                "quality": None if q is None else round(q, 3)})
        for t in detect_ticks(page, cfg):
            parent = next((tid for tid, g in zip(table_ids, grids) if _centre_in(t.bbox, g.bbox)), None)
            store.add(page.page, size, "unclassified", "checkbox", t.bbox, bbox_px=_px(t.bbox, h), parent_id=parent,
                      source=SOURCE, metadata={"state": "ticked" if t.ticked else "unticked",
                                               "method": t.method, "name": t.name})
        page_area = page.width * page.height
        for im in page.images:
            box = [im.x0, im.y0, im.x1, im.y1]
            store.add(page.page, size, "unclassified", "image", box, bbox_px=_px(box, h), source=SOURCE,
                      metadata={"width_px": im.width_px, "height_px": im.height_px,
                                "area_share": round((im.x1 - im.x0) * (im.y1 - im.y0) / page_area, 4)})
        store.pages[page.page] = {"width_pt": page.width, "height_pt": page.height,
                                  "text_quality": round(sum(qualities) / len(qualities), 3) if qualities else None,
                                  "n_chars": len(page.chars), "n_paths": len(page.rects) + len(page.lines) + len(page.curves)}
        log_event("elements", "page", page=page.page, tables=len(grids), text_blocks=len(blocks),
                  checkboxes=len(store.of_type("checkbox", page=page.page)), images=len(page.images))
    return store
