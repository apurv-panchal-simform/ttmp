"""When is Document Intelligence needed (plan section 1.3)? A pure function over Phase 1 output.

Reasons: text that exists only in pictures, scanned pages, regions with a low text-quality score, and
tables whose structure the PDF layers could not fill (a second opinion). Needs no DI call to evaluate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import IntakeCfg, load_config
from ..geometry import centre_in
from ..models import ElementStore
from ..vocab import Route


@dataclass
class Trigger:
    page: int
    reason: str            # scanned_page | low_text_quality | text_in_picture | ambiguous_table
    bbox_pt: list[float] | None
    element: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def di_triggers(store: ElementStore, cfg: IntakeCfg | None = None) -> list[Trigger]:
    cfg = cfg or load_config().intake
    out: list[Trigger] = []
    for page, info in sorted(store.pages.items()):
        if info.n_chars < cfg.min_text_chars:
            out.append(Trigger(page, "scanned_page", None, None))
    for e in store.elements.values():
        if e.type == "text_block" and "low_text_quality" in e.flags:
            out.append(Trigger(e.page, "low_text_quality", e.bbox_pt, e.element_id))
        elif e.type == "image" and e.route == Route.FIGURE:
            has_text = any(t.type in ("text_block", "page_furniture") and t.page == e.page
                           and centre_in(t.bbox_pt, e.bbox_pt) for t in store.on_page(e.page))
            if not has_text:
                out.append(Trigger(e.page, "text_in_picture", e.bbox_pt, e.element_id))
        elif e.type == "table" and e.route in (Route.TABLE, Route.RISK_TABLE):
            if not any(k.parent_id == e.element_id and k.type == "text_block" for k in store.on_page(e.page)):
                out.append(Trigger(e.page, "ambiguous_table", e.bbox_pt, e.element_id))
    return sorted(out, key=lambda t: (t.page, t.reason, t.element or ""))
