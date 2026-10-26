"""Layer data -> coarse elements (plan sections 1.4 and 1.6).

Elements start in zone "unclassified"; zones.py assigns zones per region afterwards. Text inside a table
becomes a child of that table (claimed by it), so nothing is stored twice.
"""
from __future__ import annotations

from dataclasses import asdict

from ....config import Config
from ....logging_ import log_event
from ...geometry import centre_in, rounded_px_box
from ...models import ElementStore, PageInfo
from .gridfind import detect_tables
from .quality import assess
from .reader import PageLayers
from .textblocks import build_text_blocks
from .ticks import detect_ticks

SOURCE = "pdf-layers"


def build_elements(layers: list[PageLayers], doc_id: str, cfg: Config) -> ElementStore:
    store = ElementStore(doc_id)
    low, min_words = cfg.quality.low_threshold, cfg.quality.min_words
    for page in layers:
        size = (page.width, page.height)
        h = page.height
        grids = detect_tables(page, cfg)
        table_ids: list[str] = []
        for g in grids:
            el = store.add(page.page, size, "unclassified", "table", g.bbox, bbox_px=rounded_px_box(g.bbox, h), source=SOURCE,
                           metadata={"n_xs": g.n_xs, "n_ys": g.n_ys, "n_parts": g.n_parts,
                                     "n_fills": g.n_fills, "rating_cells": g.rating_cells,
                                     "h_rules": g.h_rules, "v_rules": g.v_rules, "fills": g.fills})
            table_ids.append(el.element_id)
        blocks = build_text_blocks(page, grids, cfg)
        qualities = []
        for b in blocks:
            qa = assess(b.text)
            q = qa.score
            flags = ["low_text_quality"] if qa.is_low(low, min_words) else []
            if q is not None:
                qualities.append(q)
            store.add(page.page, size, "unclassified", "text_block", b.bbox, bbox_px=rounded_px_box(b.bbox, h), flags=flags,
                      parent_id=table_ids[b.table] if b.table is not None else None, source=SOURCE,
                      representations={"text": b.text, "lines": [asdict(ln) for ln in b.lines]},
                      metadata={"rotation": b.rotation, "band": b.band, "size": round(b.size, 1), "bold": b.bold,
                                "quality": None if q is None else round(q, 3)})
        for t in detect_ticks(page, cfg):
            parent = next((tid for tid, g in zip(table_ids, grids) if centre_in(t.bbox, g.bbox)), None)
            store.add(page.page, size, "unclassified", "checkbox", t.bbox, bbox_px=rounded_px_box(t.bbox, h), parent_id=parent,
                      source=SOURCE, metadata={"state": "ticked" if t.ticked else "unticked",
                                               "method": t.method, "name": t.name})
        page_area = page.width * page.height
        for im in page.images:
            box = [im.x0, im.y0, im.x1, im.y1]
            store.add(page.page, size, "unclassified", "image", box, bbox_px=rounded_px_box(box, h), source=SOURCE,
                      metadata={"width_px": im.width_px, "height_px": im.height_px,
                                "area_share": round((im.x1 - im.x0) * (im.y1 - im.y0) / page_area, 4)})
        store.pages[page.page] = PageInfo(
            width_pt=page.width, height_pt=page.height,
            text_quality=round(sum(qualities) / len(qualities), 3) if qualities else None,
            n_chars=len(page.chars), n_paths=len(page.rects) + len(page.lines) + len(page.curves))
        log_event("elements", "page", page=page.page, tables=len(grids), text_blocks=len(blocks),
                  checkboxes=len(store.of_type("checkbox", page=page.page)), images=len(page.images))
    return store
