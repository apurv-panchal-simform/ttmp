"""Element model and store (plan section 1.4).

Coordinates are PDF points with origin bottom-left. Pixel boxes are on a master render
(origin top-left). Ids are deterministic: p{page}:{type}:{order on page}. ``fingerprint()`` is an id that
does not depend on detection order, for caches and records that must survive a re-run with new thresholds.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..constants import MASTER_DPI
from .geometry import bbox_pt_to_px, bbox_px_to_pt, pt_to_px, px_to_pt  # noqa: F401  (re-exported)
from .vocab import ElementType, Flag, Outcome, Route, Zone

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

TYPES = tuple(t.value for t in ElementType)
ZONES = tuple(z.value for z in Zone)
FLAGS = tuple(f.value for f in Flag)

_ID_RE = re.compile(r"^p(\d+):([a-z_]+):(\d+)$")


class SchemaError(ValueError):
    """A stored file was written by a newer schema than this code understands, or is not a store at all."""


@dataclass
class Element:
    element_id: str
    doc_id: str
    page: int
    page_size_pt: list[float]
    zone: Zone
    type: ElementType
    bbox_pt: list[float]
    printed_page: int | None = None
    bbox_px: list[float] | None = None
    parent_id: str | None = None
    links: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    representations: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    source: str = ""
    # set by the router
    route: Route | None = None
    route_rule: str | None = None
    outcome: Outcome | None = None
    triage: str | None = None
    claimed_by: str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        # zone, type and flags are checked on every assignment, not only in ElementStore.add
        if name == "zone":
            value = Zone(value)
        elif name == "type":
            value = ElementType(value)
        elif name == "flags":
            value = [Flag(f) for f in value]
        elif name == "route" and value is not None:
            value = Route(value)
        elif name == "outcome" and value is not None:
            value = Outcome(value)
        object.__setattr__(self, name, value)

    def fingerprint(self) -> str:
        """Stable across re-runs: page, type, box rounded to 1 pt and the text, never the sequence number."""
        text = self.representations.get("text", "")
        key = f"{self.page}|{self.type}|{[round(v) for v in self.bbox_pt]}|{text}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class PageInfo:
    """Per-page facts that are not elements. Core fills the layer and zone fields, DI fills the di_* ones."""
    width_pt: float
    height_pt: float
    n_chars: int = 0
    n_paths: int = 0
    text_quality: float | None = None
    zone: Zone = Zone.UNCLASSIFIED
    zone_reason: str = ""
    signals: dict = field(default_factory=dict)
    form_name: str | None = None
    printed_page: int | None = None
    tmp_reference: str | None = None
    master: dict | None = None
    di_words: int | None = None
    di_words_kept: int | None = None
    di_figures: int | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "zone":
            value = Zone(value)
        object.__setattr__(self, name, value)


def _known(cls: type, raw: dict, what: str) -> dict:
    names = {f.name for f in fields(cls)}
    extra = sorted(set(raw) - names)
    if extra:
        log.warning("ignoring unknown %s field(s) %s (written by a newer version?)", what, ", ".join(extra))
    return {k: v for k, v in raw.items() if k in names}


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename, so a crash never leaves a truncated file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ElementStore:
    def __init__(self, doc_id: str = ""):
        self.doc_id = doc_id
        self.elements: dict[str, Element] = {}
        self.pages: dict[int, PageInfo] = {}   # per-page info: size, zone, signals, quality
        self.metadata: dict = {}               # document metadata promoted from page furniture
        self._counters: dict[tuple[int, str], int] = {}

    def add(self, page: int, page_size_pt, zone: Zone | str, type: ElementType | str, bbox_pt, **kw) -> Element:  # noqa: A002
        etype, ezone = ElementType(type), Zone(zone)
        explicit = kw.pop("element_id", None)
        if explicit is not None and explicit in self.elements:
            raise ValueError(f"duplicate element id {explicit!r}")
        n = self._counters.get((page, etype.value), 0)
        eid = explicit or f"p{page}:{etype.value}:{n}"
        if eid in self.elements:
            raise ValueError(f"duplicate element id {eid!r}")
        if explicit is None:
            self._counters[(page, etype.value)] = n + 1
        el = Element(
            element_id=eid, doc_id=self.doc_id, page=page,
            page_size_pt=[round(float(v), 1) for v in page_size_pt],
            zone=ezone, type=etype, bbox_pt=[round(float(v), 2) for v in bbox_pt], **kw,
        )
        self.elements[eid] = el
        return el

    def retype(self, element: Element, new_type: ElementType | str) -> Element:
        """Change an element's type and re-key it so its id still names its type.

        The element keeps its place in iteration order; ``parent_id`` and ``links`` of other elements that
        pointed at the old id are rewritten.
        """
        new = ElementType(new_type)
        if new == element.type:
            return element
        old_id = element.element_id
        n = self._counters.get((element.page, new.value), 0)
        new_id = f"p{element.page}:{new.value}:{n}"
        self._counters[(element.page, new.value)] = n + 1
        element.type = new
        element.element_id = new_id
        self.elements = {(new_id if k == old_id else k): v for k, v in self.elements.items()}
        for other in self.elements.values():
            if other.parent_id == old_id:
                other.parent_id = new_id
            if old_id in other.links:
                other.links = [new_id if link == old_id else link for link in other.links]
        return element

    def of_type(self, *types: ElementType | str, page: int | None = None) -> list[Element]:
        return [e for e in self.elements.values()
                if e.type in types and (page is None or e.page == page)]

    def on_page(self, page: int) -> list[Element]:
        return [e for e in self.elements.values() if e.page == page]

    def ensure_pixel_boxes(self, dpi: float = MASTER_DPI) -> None:
        """Give every element a pixel box on the master render (zones adds some elements without one)."""
        for e in self.elements.values():
            if e.bbox_px is None:
                e.bbox_px = [round(v, 1) for v in bbox_pt_to_px(e.bbox_pt, e.page_size_pt[1], dpi)]

    def dump(self, path: str | Path) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "doc_id": self.doc_id, "metadata": self.metadata,
                   "pages": {str(k): asdict(v) for k, v in self.pages.items()},
                   "elements": [asdict(e) for e in self.elements.values()]}
        atomic_write_text(path, json.dumps(payload, indent=1, ensure_ascii=False))

    @classmethod
    def load(cls, path: str | Path) -> ElementStore:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        version = data.get("schema_version", 0)          # files from before versioning are treated as 0
        if version > SCHEMA_VERSION:
            raise SchemaError(f"{path}: schema_version {version} is newer than supported {SCHEMA_VERSION}")
        store = cls(data["doc_id"])
        store.metadata = data.get("metadata", {})
        store.pages = {int(k): PageInfo(**_known(PageInfo, v, "page")) for k, v in data.get("pages", {}).items()}
        for raw in data["elements"]:
            el = Element(**_known(Element, raw, "element"))
            store.elements[el.element_id] = el
        # resume auto-numbering after the highest order of every id, whatever the element's current type
        for eid in store.elements:
            m = _ID_RE.match(eid)
            if m:
                key = (int(m.group(1)), m.group(2))
                store._counters[key] = max(store._counters.get(key, 0), int(m.group(3)) + 1)
        return store
