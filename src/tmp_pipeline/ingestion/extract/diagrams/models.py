"""What reading a diagram sheet produces: symbols, legend, notes, regions, image lineage, title block.

``SheetStore`` is the hand-off from the pre-router diagram stage to the diagram handler: the stage owns the
element store, the handler only reads these models. Everything is plain data so a sheet survives a JSON round trip.
"""
from __future__ import annotations

import colorsys
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ...models import atomic_write_text
from ...vocab import SymbolKind

SCHEMA_VERSION = 1
Colour = tuple[int, int, int]


def colour_name(c: Colour | None) -> str | None:
    """A plain colour word for an RGB triple, used to describe legend swatches."""
    if c is None:
        return None
    r, g, b = (v / 255 for v in c)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if v < 0.18:
        return "black"
    if s < 0.12:
        return "white" if v > 0.9 else "grey"
    deg = h * 360
    if deg < 10 or deg >= 345:
        return "red"
    if deg < 45:
        return "orange"
    if deg < 70:
        return "yellow"
    if deg < 170:
        return "green"
    if deg < 260:
        return "blue"
    return "purple"


@dataclass
class Symbol:
    id: str                              # the element id of its vector_symbol element
    kind: SymbolKind
    bbox: list[float]                    # page points
    text: str = ""                       # its text lines joined with a space, as drawn
    lines: list[str] = field(default_factory=list)
    value: str | float | int | None = None
    unit: str | None = None
    meaning: str | None = None           # legend meaning for areas and coloured lines
    colours: list[Colour] = field(default_factory=list)
    objects: list[int] = field(default_factory=list)       # drawing-order indices of its objects
    angle: float | None = None           # reading direction of its text
    rp: float | None = None              # nearest RP label
    rp_id: str | None = None
    road: str | None = None              # nearest road label name
    road_level: str | None = None
    temporary: bool = False              # a TEMPORARY plate belongs to it
    region: str = "plan"                 # plan | inset
    count: int = 1                       # cones in a run
    needs_caption: bool = False
    low_text_quality: bool = False
    order: int | None = None             # position in sign order along its approach
    approach: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LegendEntry:
    swatch: str                          # "green", "blue dashed", "grey hatched"
    meaning: str
    fill: Colour | None = None
    stroke: Colour | None = None
    dashed: bool = False
    bbox: list[float] = field(default_factory=list)


@dataclass
class Region:
    kind: str                            # frame | legend | notes | title_block | inset | figure | callout
    bbox: list[float]
    label: str | None = None
    element_id: str | None = None
    text: str = ""


@dataclass
class ImageInfo:
    index: int
    bbox: list[float]
    px: tuple[int, int]
    phash: str | None
    role: str                            # aerial | inset | figure | decorative
    level: int = 0
    reused_on: list[int] = field(default_factory=list)   # other pages with the same picture
    element_id: str | None = None


@dataclass
class SheetModel:
    page: int
    vector: bool                                         # a vector overlay was found (False: flattened or scanned)
    sheet: str | None = None
    title: str | None = None
    rev: str | None = None
    title_block: dict[str, str] = field(default_factory=dict)
    legend: list[LegendEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    callouts: list[str] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    images: list[ImageInfo] = field(default_factory=list)
    roads: list[dict[str, str]] = field(default_factory=list)
    low_quality_lines: list[str] = field(default_factory=list)
    sheet_element_id: str | None = None
    master: str | None = None
    overlay: str | None = None
    paths: int = 0
    dropped_lines: int = 0               # unlabelled leader and extension lines left out of the symbol list

    def symbols_of(self, *kinds: SymbolKind) -> list[Symbol]:
        return [s for s in self.symbols if s.kind in kinds]


class SheetStore:
    """Sheet models keyed by page number."""

    def __init__(self) -> None:
        self.sheets: dict[int, SheetModel] = {}

    def add(self, sheet: SheetModel) -> None:
        self.sheets[sheet.page] = sheet

    def get(self, page: int) -> SheetModel | None:
        return self.sheets.get(page)

    def __len__(self) -> int:
        return len(self.sheets)

    def __iter__(self):
        return iter(self.sheets.values())

    def dump(self, path: str | Path) -> None:
        atomic_write_text(path, json.dumps(
            {"schema_version": SCHEMA_VERSION, "sheets": [asdict(s) for s in self.sheets.values()]},
            indent=1, ensure_ascii=False))

    @classmethod
    def load(cls, path: str | Path) -> SheetStore:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        for s in raw["sheets"]:
            store.add(_sheet_from_dict(s))
        return store


def _pick(cls: type, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _sheet_from_dict(d: dict) -> SheetModel:
    d = dict(d)
    d["legend"] = [LegendEntry(**_pick(LegendEntry, e)) for e in d.get("legend", [])]
    d["regions"] = [Region(**_pick(Region, r)) for r in d.get("regions", [])]
    d["images"] = [ImageInfo(**_pick(ImageInfo, i)) for i in d.get("images", [])]
    d["symbols"] = [Symbol(**{**_pick(Symbol, s), "kind": SymbolKind(s["kind"])}) for s in d.get("symbols", [])]
    return SheetModel(**_pick(SheetModel, d))
