"""Zones per region (plan section 1.5).

A zone says what kind of content a region is. The page gets a default zone from its signals; a region with
a stronger signal (a block of rating-colour cells) overrides it, so page 5 of the sample is forms on top
and risk below. Regions with no usable signal stay "unclassified" and are flagged for the (deferred)
vision-model fallback. Cues come from the template config.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .geometry import centre_in, in_band
from .logging_ import log_event
from .models import ElementStore
from .vocab import Zone


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


def page_signals(store: ElementStore, page: int, cfg: Config) -> PageSignals:
    z, fz = cfg.zones, cfg.furniture
    sig = PageSignals(page)
    info = store.pages[page]
    els = store.on_page(page)
    texts = [e for e in els if e.type == "text_block"]
    header = " ".join(e.representations["text"] for e in texts if e.metadata["band"] == "header").replace("\n", " ")
    footer = " ".join(e.representations["text"] for e in texts if e.metadata["band"] == "footer").replace("\n", " ")
    everything = " ".join(e.representations["text"] for e in texts)
    sig.header_band = bool(z.header_band_regex.search(header))
    m = fz.form_name_regex.search(footer) or fz.form_name_regex.search(header)
    if m:
        sig.form_name, sig.form_kind = m.group(0), m.group(2).strip().lower()
    sig.image_cover = max([e.metadata["area_share"] for e in els if e.type == "image"] or [0.0])
    sig.n_paths = info.n_paths
    sig.sheet_no = bool(z.sheet_no_regex.search(everything))
    sig.generic_code = bool(z.generic_code_regex.search(everything))
    sig.body_content = (
        any(e.metadata["band"] == "body" for e in texts)
        or bool(store.of_type("checkbox", page=page))
        or any(e.metadata["area_share"] >= cfg.figures.decorative_max_area for e in els if e.type == "image"))
    h = info.height_pt
    body_tables = [t for t in els if t.type == "table"
                   and in_band(t.bbox_pt, h, cfg.page.header_band_pt, cfg.page.footer_band_pt) is None]
    total = sum((t.bbox_pt[2] - t.bbox_pt[0]) * (t.bbox_pt[3] - t.bbox_pt[1]) for t in body_tables)
    risk = [t for t in body_tables if t.metadata["rating_cells"] >= z.rating_min_cells]
    risk_area = sum((t.bbox_pt[2] - t.bbox_pt[0]) * (t.bbox_pt[3] - t.bbox_pt[1]) for t in risk)
    sig.risk_regions = [t.bbox_pt for t in risk]
    sig.risk_share = risk_area / total if total else 0.0
    return sig


def page_default(sig: PageSignals, cfg: Config) -> tuple[str, str]:
    z = cfg.zones
    if sig.image_cover > z.diagram_image_cover and not sig.header_band and (sig.n_paths >= z.diagram_min_paths or sig.sheet_no):
        if sig.n_paths < z.diagram_min_paths:        # a flattened sheet: the drawing is pixels, only the title block is text
            return "diagram", f"image covers {sig.image_cover:.0%} of the page with a SHEET NO title block and only {sig.n_paths} vector paths (flattened)"
        why = f"image covers {sig.image_cover:.0%} of the page with {sig.n_paths} vector paths"
        return "diagram", why + (" and a SHEET NO title block" if sig.sheet_no else "")
    if (sig.header_band or sig.form_name) and not sig.body_content:
        return "blank", "only header and footer text"
    if not sig.header_band:
        if sig.generic_code:
            return "generic_standard", "no shared header band, template code present"
        return "unclassified", "no header band and no other signal"
    if sig.risk_regions and sig.risk_share >= z.risk_page_table_share:
        return "risk", f"rating-colour cells in {sig.risk_share:.0%} of the body table area"
    if sig.form_kind in z.form_kind:
        return z.form_kind[sig.form_kind], f"form type '{sig.form_kind}'"
    return "unclassified", "header band but no recognised form name"


def assign_zones(store: ElementStore, cfg: Config) -> None:
    """Set the zone of every element, add region elements for overrides and diagram_sheet elements."""
    for page in sorted(store.pages):
        sig = page_signals(store, page, cfg)
        zone, why = page_default(sig, cfg)
        info = store.pages[page]
        size = (info.width_pt, info.height_pt)
        regions = [] if zone in ("risk", "diagram", "blank") else list(sig.risk_regions)
        for bbox in regions:
            store.add(page, size, "risk", "region", bbox, source="zones",
                      metadata={"zone": "risk", "signal": "rating-colour cells", "page_default": zone})
        if zone == "diagram":
            store.add(page, size, "diagram", "diagram_sheet", [0, 0, size[0], size[1]], source="zones",
                      metadata={"image_cover": round(sig.image_cover, 3), "overlay_paths": sig.n_paths,
                                "sheet_no_title_block": sig.sheet_no})
        table_zone: dict[str, Zone] = {}
        for e in store.on_page(page):
            if e.type in ("region", "diagram_sheet"):
                continue
            if e.parent_id and e.parent_id in table_zone:
                e.zone = table_zone[e.parent_id]
                continue
            e.zone = Zone(zone)
            for bbox in regions:
                if centre_in(e.bbox_pt, bbox):
                    e.zone = Zone.RISK
            if e.type == "table":
                table_zone[e.element_id] = e.zone
        info.zone, info.zone_reason, info.form_name = Zone(zone), why, sig.form_name
        info.signals = {"header_band": sig.header_band, "image_cover": round(sig.image_cover, 3),
                        "n_paths": sig.n_paths, "risk_share": round(sig.risk_share, 3),
                        "sheet_no": sig.sheet_no, "generic_code": sig.generic_code}
        log_event("zones", "page", page=page, zone=zone, reason=why, risk_regions=len(regions))
