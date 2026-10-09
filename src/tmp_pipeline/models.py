"""Element model and store (plan section 1.4).

Coordinates are PDF points with origin bottom-left. Pixel boxes are on a master render
(origin top-left). Ids are deterministic: p{page}:{type}:{order on page}.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

TYPES = (
    "heading", "paragraph", "list_item", "table", "table_row", "kv_field", "checkbox", "numeric",
    "identifier", "image", "figure", "photo", "diagram_sheet", "vector_symbol", "legend", "notes", "region",
    # Phase 1 coarse types
    "text_block", "page_furniture", "ocr_word",
)
ZONES = ("forms", "risk", "narrative", "diagram", "generic_standard", "blank", "unclassified")
FLAGS = (
    "struck", "duplicate", "boilerplate", "generic", "contact_info", "id_conflict", "rating_mismatch",
    "needs_caption", "low_text_quality", "leftover", "decorative",
)


@dataclass
class Element:
    element_id: str
    doc_id: str
    page: int
    page_size_pt: list[float]
    zone: str
    type: str
    bbox_pt: list[float]
    printed_page: int | None = None
    bbox_px: list[float] | None = None
    parent_id: str | None = None
    links: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    representations: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    source: str = ""


class ElementStore:
    def __init__(self, doc_id: str = ""):
        self.doc_id = doc_id
        self.elements: dict[str, Element] = {}
        self.pages: dict[int, dict] = {}       # per-page info: size, zone, signals, quality
        self.metadata: dict = {}               # document metadata promoted from page furniture
        self._counters: dict[tuple[int, str], int] = {}

    def add(self, page: int, page_size_pt, zone: str, type: str, bbox_pt, **kw) -> Element:
        if type not in TYPES:
            raise ValueError(f"unknown element type {type!r}")
        if zone not in ZONES:
            raise ValueError(f"unknown zone {zone!r}")
        for f in kw.get("flags", []):
            if f not in FLAGS:
                raise ValueError(f"unknown flag {f!r}")
        explicit = kw.pop("element_id", None)
        if explicit is not None and explicit in self.elements:
            raise ValueError(f"duplicate element id {explicit!r}")
        n = self._counters.get((page, type), 0)
        eid = explicit or f"p{page}:{type}:{n}"
        if eid in self.elements:
            raise ValueError(f"duplicate element id {eid!r}")
        if explicit is None:
            self._counters[(page, type)] = n + 1
        el = Element(
            element_id=eid, doc_id=self.doc_id, page=page,
            page_size_pt=[round(float(v), 1) for v in page_size_pt],
            zone=zone, type=type, bbox_pt=[round(float(v), 2) for v in bbox_pt], **kw,
        )
        self.elements[eid] = el
        return el

    def of_type(self, *types: str, page: int | None = None) -> list[Element]:
        return [e for e in self.elements.values()
                if e.type in types and (page is None or e.page == page)]

    def on_page(self, page: int) -> list[Element]:
        return [e for e in self.elements.values() if e.page == page]

    def ensure_pixel_boxes(self, dpi: float = 200) -> None:
        """Give every element a pixel box on the master render (zones adds some elements without one)."""
        for e in self.elements.values():
            if e.bbox_px is None:
                e.bbox_px = [round(v, 1) for v in bbox_pt_to_px(e.bbox_pt, e.page_size_pt[1], dpi)]

    def dump(self, path: str | Path) -> None:
        payload = {"doc_id": self.doc_id, "metadata": self.metadata,
                   "pages": {str(k): v for k, v in self.pages.items()},
                   "elements": [asdict(e) for e in self.elements.values()]}
        Path(path).write_text(json.dumps(payload, indent=1, ensure_ascii=False))

    @classmethod
    def load(cls, path: str | Path) -> "ElementStore":
        data = json.loads(Path(path).read_text())
        store = cls(data["doc_id"])
        store.metadata = data.get("metadata", {})
        store.pages = {int(k): v for k, v in data.get("pages", {}).items()}
        for raw in data["elements"]:
            el = Element(**raw)
            store.elements[el.element_id] = el
            # resume auto-numbering after the highest existing order for this page/type
            prefix = f"p{el.page}:{el.type}:"
            if el.element_id.startswith(prefix) and el.element_id[len(prefix):].isdigit():
                key = (el.page, el.type)
                store._counters[key] = max(store._counters.get(key, 0), int(el.element_id[len(prefix):]) + 1)
        return store


def pt_to_px(x_pt: float, y_pt: float, page_h_pt: float, dpi: float) -> tuple[float, float]:
    s = dpi / 72
    return x_pt * s, (page_h_pt - y_pt) * s


def px_to_pt(u: float, v: float, page_h_pt: float, dpi: float) -> tuple[float, float]:
    s = dpi / 72
    return u / s, page_h_pt - v / s


def bbox_pt_to_px(b, page_h_pt: float, dpi: float) -> list[float]:
    """[x0, y0, x1, y1] in points (bottom-left origin) -> [u0, v0, u1, v1] pixels (top-left origin)."""
    u0, v1 = pt_to_px(b[0], b[1], page_h_pt, dpi)
    u1, v0 = pt_to_px(b[2], b[3], page_h_pt, dpi)
    return [u0, v0, u1, v1]


def bbox_px_to_pt(b, page_h_pt: float, dpi: float) -> list[float]:
    x0, y1 = px_to_pt(b[0], b[1], page_h_pt, dpi)
    x1, y0 = px_to_pt(b[2], b[3], page_h_pt, dpi)
    return [x0, y0, x1, y1]
