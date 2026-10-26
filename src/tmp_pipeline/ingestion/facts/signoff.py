"""Sign-off blocks and approval status (plan section 3.3, "Approval status").

A sign-off block is a table whose grid holds a "Signature" label. Its role comes from the text just above the table
(and the statement in its first row), matched against ``[facts.signoff_roles]``. The grid is read by label: the value
of a label is the cell below it, so a block read as a records table with shifted columns is still read correctly.
A "Checked by" label row starts the checker's block inside the designer's table.

A block is signed only when its signature cell has content; a date alone is not a signature. A struck-through
block is not applicable. All required blocks signed -> signed; some -> partially_signed; none -> unsigned.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...config import FactsCfg
from ...records import Record, RecordStore
from ..catalog import Catalog
from ..geometry import centre_in
from ..models import Element, ElementStore
from ..numerics import extract
from ..vocab import ElementType, Flag
from .common import source_of
from .model import Approval, Signoff

Grid = dict[int, dict[int, str]]


def table_grid(table: Record, records: RecordStore) -> Grid:
    """The cell grid of a table record: its own cells, or for a records table its header row plus its row records."""
    grid: Grid = {}
    for c in table.data.get("cells") or []:
        grid.setdefault(int(c["row"]), {})[int(c["col"])] = str(c.get("text") or "")
    if grid:
        return grid
    tid = table.data.get("table")
    if table.data.get("header_rows"):
        grid[0] = {i: str(h or "") for i, h in enumerate(table.data.get("headers") or [])}
    for r in records.of_kind("table_row", page=table.page):
        if r.data.get("table") == tid:
            for c in r.data.get("cells") or []:
                grid.setdefault(int(c["row"]), {})[int(c["col"])] = str(c.get("text") or "")
    return grid


@dataclass
class Block:
    role: str
    values: dict[str, str] = field(default_factory=dict)


def _label(text: str, cfg: FactsCfg) -> str | None:
    t = " ".join(text.split())
    return next((k for k, rx in cfg.signoff_labels.items() if rx.search(t)), None)


def read_blocks(grid: Grid, role: str, cfg: FactsCfg) -> tuple[list[Block], str]:
    """The blocks in one sign-off grid and the statement text of its first row (if any)."""
    blocks = [Block(role)]
    statement = []
    for r in sorted(grid):
        cells = {c: t for c, t in grid[r].items() if t.strip()}
        if not cells:
            continue
        labels = {c: _label(t, cfg) for c, t in cells.items()}
        if not all(labels.values()):
            if not any(labels.values()) and all(_label(t, cfg) is None for t in grid.get(r - 1, {}).values()):
                statement += list(cells.values())          # free text, not a value row under a label row
            continue
        if "checked_by" in labels.values():
            blocks.append(Block(cfg.checked_by_role))
        below = grid.get(r + 1, {})
        if below and any(_label(t, cfg) for t in below.values() if t.strip()):
            below = {}                                     # the next row is another label row: these are empty
        for c, lab in labels.items():
            name = "name" if lab == "checked_by" else str(lab)
            blocks[-1].values[name] = below.get(c, "").strip()
    return [b for b in blocks if b.values], " ".join(statement)


def _heading_text(store: ElementStore, table_el: Element, cfg: FactsCfg) -> tuple[str, list[Element]]:
    """Text blocks just above the table (within ``heading_max_gap_pt``) that are not inside another table."""
    page_tables = store.of_type(ElementType.TABLE, page=table_el.page)
    top = table_el.bbox_pt[3]
    above = []
    for e in store.of_type(ElementType.TEXT_BLOCK, page=table_el.page):
        if not (top - 1 <= e.bbox_pt[1] <= top + cfg.heading_max_gap_pt):
            continue
        if any(centre_in(e.bbox_pt, t.bbox_pt) for t in page_tables):
            continue
        above.append(e)
    above.sort(key=lambda e: -e.bbox_pt[1])
    return " ".join(str(e.representations.get("text") or "") for e in above), above


def _struck(store: ElementStore, table_el: Element, heading: list[Element]) -> bool:
    nearest = heading[-1:] if heading else []
    if any(Flag.STRUCK in e.flags for e in nearest):
        return True
    inside = [e for e in store.of_type(ElementType.TEXT_BLOCK, page=table_el.page)
              if centre_in(e.bbox_pt, table_el.bbox_pt)]
    return bool(inside) and sum(Flag.STRUCK in e.flags for e in inside) * 2 > len(inside)


@dataclass
class SignoffTable:
    record: Record
    statement: str
    signoffs: list[Signoff]


def read_signoffs(store: ElementStore, records: RecordStore, cfg: FactsCfg) -> list[SignoffTable]:
    out = []
    sig = cfg.signoff_labels["signature"]
    for table in sorted(records.of_kind("table"), key=lambda r: (r.page, r.record_id)):
        grid = table_grid(table, records)
        if not any(sig.search(" ".join(t.split())) for row in grid.values() for t in row.values()):
            continue
        el = store.elements.get(str(table.data.get("table") or ""))
        heading, heading_els = _heading_text(store, el, cfg) if el else ("", [])
        _, statement = read_blocks(grid, "", cfg)
        cue = f"{heading} {statement}"
        role = next((k for k, rx in cfg.signoff_roles.items() if rx.search(cue)), None)
        struck = bool(el) and _struck(store, el, heading_els)       # type: ignore[arg-type]
        blocks, _ = read_blocks(grid, role or "unknown", cfg)
        src = source_of(table)
        src.element_ids = [*(e.element_id for e in heading_els), *src.element_ids]
        sos = []
        for b in blocks:
            v = {k: (val or None) for k, val in b.values.items()}
            notes = [] if role else ["no role cue above the block"]
            if struck:
                status = "not_applicable"
                notes.append("the block is struck through")
            elif _is_signature(v.get("signature")):
                status = "signed"
            else:
                status = "unsigned"
                notes.append("the signature cell is empty" if not v.get("signature") else
                             "the signature cell holds only a date")
            sos.append(Signoff(b.role, table.page, status, v.get("name"), v.get("organisation"), v.get("email"),
                               v.get("phone"), v.get("signature"), v.get("date"), [src], notes))
        out.append(SignoffTable(table, statement, sos))
    return out


def _is_signature(text: str | None) -> bool:
    if not text or not text.strip():
        return False
    dates = extract(text, ("date",))
    rest = text
    for n in dates:
        rest = rest.replace(n.raw, " ")
    return any(ch.isalpha() for ch in rest)


def approval(signoffs: list[Signoff], catalog: Catalog, conditions: dict[str, bool]) -> Approval:
    by_role: dict[str, Signoff] = {}
    for s in signoffs:
        by_role.setdefault(s.role, s)
    required: list[str] = []
    signed: list[str] = []
    unsigned: list[str] = []
    na: list[str] = []
    notes: list[str] = []
    for req in catalog.required_signoffs:
        if req.condition and not conditions.get(req.condition, False):
            continue
        block = by_role.get(req.role)
        if block is not None and block.status == "not_applicable":
            na.append(req.role)
            continue
        required.append(req.role)
        if block is None:
            unsigned.append(req.role)
            notes.append(f"no sign-off block found for {req.role}")
        elif block.status == "signed":
            signed.append(req.role)
        else:
            unsigned.append(req.role)
    na += [s.role for s in signoffs if s.status == "not_applicable" and s.role not in na]
    status = "signed" if required and not unsigned else "partially_signed" if signed else "unsigned"
    return Approval(status, required, signed, unsigned, na, notes)

