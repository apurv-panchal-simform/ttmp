"""When is Document Intelligence needed (plan section 1.3)? A pure function over Phase 1 output.

Reasons: text that exists only in pictures, scanned pages, regions with a low text-quality score, and
tables whose structure the PDF layers could not fill (a second opinion). Needs no DI call to evaluate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..intake import MIN_TEXT_CHARS
from ..models import ElementStore


@dataclass
class Trigger:
    page: int
    reason: str            # scanned_page | low_text_quality | text_in_picture | ambiguous_table
    bbox_pt: list[float] | None
    element: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _centre_in(inner, outer) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def di_triggers(store: ElementStore) -> list[Trigger]:
    out: list[Trigger] = []
    for page, info in sorted(store.pages.items()):
        if info["n_chars"] < MIN_TEXT_CHARS:
            out.append(Trigger(page, "scanned_page", None, None))
    for e in store.elements.values():
        if e.type == "text_block" and "low_text_quality" in e.flags:
            out.append(Trigger(e.page, "low_text_quality", e.bbox_pt, e.element_id))
        elif e.type == "image" and e.metadata.get("route") == "FIGURE":
            has_text = any(t.type in ("text_block", "page_furniture") and t.page == e.page
                           and _centre_in(t.bbox_pt, e.bbox_pt) for t in store.on_page(e.page))
            if not has_text:
                out.append(Trigger(e.page, "text_in_picture", e.bbox_pt, e.element_id))
        elif e.type == "table" and e.metadata.get("route") in ("TABLE", "RISK_TABLE"):
            if not any(k.parent_id == e.element_id and k.type == "text_block" for k in store.on_page(e.page)):
                out.append(Trigger(e.page, "ambiguous_table", e.bbox_pt, e.element_id))
    return sorted(out, key=lambda t: (t.page, t.reason, t.element or ""))
