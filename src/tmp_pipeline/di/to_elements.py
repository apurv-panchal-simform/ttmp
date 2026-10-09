"""DI result -> elements in the same schema, source "azure-di" (plan section 1.3).

DI words keep their DI index as the element id (p{page}:ocr_word:{index}), so image prompts can cite a word
and a check can find it again. Words inside a DI figure become children of that figure.
"""
from __future__ import annotations

from ..logging_ import log_event
from ..models import ElementStore, bbox_pt_to_px
from .geometry import check_page_geometry, polygon_to_bbox_pt

SOURCE = "azure-di prebuilt-layout 2024-11-30"
MASTER_DPI = 200


def _centre_in(b, outer) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def di_to_elements(result: dict, core: ElementStore, regions: dict[int, list[list[float]]] | None = None) -> ElementStore:
    """Build a DI element store aligned to the core store's pages.

    regions: page -> boxes (points) to keep words from; None keeps every word. Words inside figures are kept.
    """
    store = ElementStore(core.doc_id)
    figures_by_page: dict[int, list] = {}
    for fig in result.get("figures", []):
        for br in fig.get("boundingRegions", [])[:1]:
            figures_by_page.setdefault(br["pageNumber"], []).append((fig, br))
    for di_page in result.get("pages", []):
        n = di_page["pageNumber"]
        info = core.pages.get(n)
        if info is None:
            log_event("di", "page_skipped", page=n, reason="page not in the core store")
            continue
        w, h = info["width_pt"], info["height_pt"]
        problems = check_page_geometry(di_page, w, h)
        if problems:
            log_event("di", "geometry_rejected", page=n, problems=problems)
            continue
        zone = info.get("zone", "unclassified")
        fig_ids: list[tuple[str, list[float]]] = []
        for fig, br in figures_by_page.get(n, []):
            box = polygon_to_bbox_pt(br["polygon"], h)
            el = store.add(n, (w, h), zone, "figure", box, bbox_px=[round(v, 1) for v in bbox_pt_to_px(box, h, MASTER_DPI)],
                           source=SOURCE, metadata={"di_figure_id": fig.get("id")})
            fig_ids.append((el.element_id, box))
        kept = 0
        for i, word in enumerate(di_page.get("words", [])):
            box = polygon_to_bbox_pt(word["polygon"], h)
            parent = next((fid for fid, fb in fig_ids if _centre_in(box, fb)), None)
            if regions is not None and parent is None and not any(_centre_in(box, r) for r in regions.get(n, [])):
                continue
            store.add(n, (w, h), zone, "ocr_word", box, element_id=f"p{n}:ocr_word:{i}", parent_id=parent,
                      bbox_px=[round(v, 1) for v in bbox_pt_to_px(box, h, MASTER_DPI)], source=SOURCE,
                      representations={"text": word["content"]},
                      metadata={"di_index": i, "confidence": word.get("confidence")})
            kept += 1
        store.pages[n] = {"width_pt": w, "height_pt": h, "zone": zone, "di_words": len(di_page.get("words", [])),
                          "di_words_kept": kept, "di_figures": len(fig_ids)}
        log_event("di", "page_elements", page=n, figures=len(fig_ids), words=kept)
    return store
