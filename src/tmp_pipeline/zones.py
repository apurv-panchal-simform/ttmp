"""Zones per region (plan section 1.5).

A zone says what kind of content a region is. The page gets a default zone from its signals; a region with
a stronger signal (a block of rating-colour cells) overrides it, so page 5 of the sample is forms on top
and risk below. Regions with no usable signal stay "unclassified" and are flagged for the (deferred)
vision-model fallback. Cues come from the template config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logging_ import log_event
from .models import ElementStore


@dataclass
class PageSignals:
    page: int
    header_band: bool = False
    form_name: str | None = None
    form_kind: str | None = None
    image_cover: float = 0.0
    n_paths: int = 0
    sheet_no: bool = False
    generic_code: bool = False
    body_content: bool = False
    risk_regions: list[list[float]] = field(default_factory=list)
    risk_share: float = 0.0


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _inside(point, box) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def page_signals(store: ElementStore, page: int, cfg: dict) -> PageSignals:
    z, fz = cfg["zones"], cfg["furniture"]
    sig = PageSignals(page)
    info = store.pages[page]
    area = info["width_pt"] * info["height_pt"]
    els = store.on_page(page)
    texts = [e for e in els if e.type == "text_block"]
    header = " ".join(e.representations["text"] for e in texts if e.metadata["band"] == "header").replace("\n", " ")
    footer = " ".join(e.representations["text"] for e in texts if e.metadata["band"] == "footer").replace("\n", " ")
    everything = " ".join(e.representations["text"] for e in texts)
    sig.header_band = bool(re.search(z["header_band_regex"], header, re.I))
    m = re.search(fz["form_name_regex"], footer, re.I) or re.search(fz["form_name_regex"], header, re.I)
    if m:
        sig.form_name, sig.form_kind = m.group(0), m.group(2).strip().lower()
    sig.image_cover = max([e.metadata["area_share"] for e in els if e.type == "image"] or [0.0])
    sig.n_paths = info["n_paths"]
    sig.sheet_no = bool(re.search(z["sheet_no_regex"], everything, re.I))
    sig.generic_code = bool(re.search(z["generic_code_regex"], everything))
    sig.body_content = (
        any(e.metadata["band"] == "body" for e in texts)
        or bool(store.of_type("checkbox", page=page))
        or any(e.metadata["area_share"] >= cfg["figures"]["decorative_max_area"] for e in els if e.type == "image"))
    h = info["height_pt"]
    body_tables = [t for t in els if t.type == "table"
                   and not (_centre(t.bbox_pt)[1] > h - cfg["page"]["header_band_pt"] or _centre(t.bbox_pt)[1] < cfg["page"]["footer_band_pt"])]
    total = sum((t.bbox_pt[2] - t.bbox_pt[0]) * (t.bbox_pt[3] - t.bbox_pt[1]) for t in body_tables)
    risk = [t for t in body_tables if t.metadata["rating_cells"] >= z["rating_min_cells"]]
    risk_area = sum((t.bbox_pt[2] - t.bbox_pt[0]) * (t.bbox_pt[3] - t.bbox_pt[1]) for t in risk)
    sig.risk_regions = [t.bbox_pt for t in risk]
    sig.risk_share = risk_area / total if total else 0.0
    return sig


def page_default(sig: PageSignals, cfg: dict) -> tuple[str, str]:
    z = cfg["zones"]
    if sig.image_cover > z["diagram_image_cover"] and sig.n_paths >= z["diagram_min_paths"] and not sig.header_band:
        why = f"image covers {sig.image_cover:.0%} of the page with {sig.n_paths} vector paths"
        return "diagram", why + (" and a SHEET NO title block" if sig.sheet_no else "")
    if (sig.header_band or sig.form_name) and not sig.body_content:
        return "blank", "only header and footer text"
    if not sig.header_band:
        if sig.generic_code:
            return "generic_standard", "no shared header band, template code present"
        return "unclassified", "no header band and no other signal"
    if sig.risk_regions and sig.risk_share >= z["risk_page_table_share"]:
        return "risk", f"rating-colour cells in {sig.risk_share:.0%} of the body table area"
    if sig.form_kind in z["form_kind"]:
        return z["form_kind"][sig.form_kind], f"form type '{sig.form_kind}'"
    return "unclassified", "header band but no recognised form name"


def assign_zones(store: ElementStore, cfg: dict) -> None:
    """Set the zone of every element, add region elements for overrides and diagram_sheet elements."""
    for page in sorted(store.pages):
        sig = page_signals(store, page, cfg)
        zone, why = page_default(sig, cfg)
        info = store.pages[page]
        size = (info["width_pt"], info["height_pt"])
        regions = [] if zone in ("risk", "diagram", "blank") else list(sig.risk_regions)
        for bbox in regions:
            store.add(page, size, "risk", "region", bbox, source="zones",
                      metadata={"zone": "risk", "signal": "rating-colour cells", "page_default": zone})
        if zone == "diagram":
            store.add(page, size, "diagram", "diagram_sheet", [0, 0, size[0], size[1]], source="zones",
                      metadata={"image_cover": round(sig.image_cover, 3), "overlay_paths": sig.n_paths,
                                "sheet_no_title_block": sig.sheet_no})
        table_zone: dict[str, str] = {}
        for e in store.on_page(page):
            if e.type in ("region", "diagram_sheet"):
                continue
            if e.parent_id and e.parent_id in table_zone:
                e.zone = table_zone[e.parent_id]
                continue
            e.zone = zone
            for bbox in regions:
                if _inside(_centre(e.bbox_pt), bbox):
                    e.zone = "risk"
            if e.type == "table":
                table_zone[e.element_id] = e.zone
        info.update(zone=zone, zone_reason=why, form_name=sig.form_name,
                    signals={"header_band": sig.header_band, "image_cover": round(sig.image_cover, 3),
                             "n_paths": sig.n_paths, "risk_share": round(sig.risk_share, 3),
                             "sheet_no": sig.sheet_no, "generic_code": sig.generic_code})
        log_event("zones", "page", page=page, zone=zone, reason=why, risk_regions=len(regions))
