"""Table handler (plan section 2.2): a ruled grid becomes typed records.

Steps, all code except the X2 hook for names the catalog does not know:
  grid        cells from ruling segments, merged cells from missing rules, text placed per line (grid.py)
  headers     rows at the top whose cells all carry a header fill colour (a template cue); several header rows
              combine into one name per column
  shape       records table (header + rows), label/value grid, checkbox row, header block, or unknown
  names       catalog synonyms first, then X2 (cached per table); the model never supplies a value
  values      raw text kept, clean typed value added; "See Above" is refers_elsewhere; merged cells give their
              value to every row they cover (flag merged); value checks flag problems, never fix them
  stitching   a table on the next page with the same column positions continues the one before; a row whose first
              cell is empty after a page break continues the previous row
Checkboxes inside a table belong to the checkbox handler; this handler skips them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .... import logging_ as lg
from ....llm import ask
from ....records import Evidence, FieldValue, Record, Status
from ... import prompts
from ...catalog import EntityMatch, FieldDef, normalise
from ...contracts import BaseHandler, ElementUpdate, HandlerContext, HandlerResult
from ...geometry import in_band
from ...models import Element
from ...numerics import extract, parse_value
from ...validators import check_x2
from ...vocab import ElementType, Flag, Route
from .checkboxes import snake
from .grid import Cell, CellLine, Grid, build_grid

REFERS_PREFIXES = ("see above", "as above", "see below", "as per above", "see previous", "refer above", "refer to above")
NOT_APPLICABLE = {"n a", "na", "nil", "none", "-", ""}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_INLINE_KV = re.compile(r"^(?P<label>[^:]{2,70}?):\s*(?P<value>\S.*)$")
_PARENS = re.compile(r"\(([^)]+)\)")


@dataclass
class KV:
    label: str
    value: str
    group: str | None
    cell: Cell
    lines: list[CellLine]
    group_lines: list[CellLine] = field(default_factory=list)   # the heading the pair sits under, for evidence
    line: int = 0                       # visual line inside the cell: pairs on one grid row and line read as one row
    value_lines: list[str] = field(default_factory=list)        # a value written over several lines, line by line


@dataclass
class TableInfo:
    element: Element
    grid: Grid
    kids: list[Element]
    header_rows: int
    headers: list[str]
    shape: str
    kv: list[KV] = field(default_factory=list)
    continues: TableInfo | None = None        # the table on the previous page this one continues
    rows: list[Record] = field(default_factory=list)


def _close(a: tuple[float, ...], b: tuple[float, ...], tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _unique(cells: list[Cell]) -> list[Cell]:
    seen: set[int] = set()
    out = []
    for c in cells:
        if id(c) not in seen:
            seen.add(id(c))
            out.append(c)
    return out


def _is_header_row(grid: Grid, r: int, fills, tol: float) -> bool:
    cells = _unique(grid.row_slots(r))
    return bool(cells) and all(c.fill is not None and any(_close(c.fill, f, tol) for f in fills) for c in cells)


def _is_group_row(grid: Grid, r: int, fills, tol: float) -> bool:
    """A header-fill row made of one cell across the whole table labels the rows beneath it; it names no column."""
    cells = _unique(grid.row_slots(r))
    return (grid.n_cols >= 3 and len(cells) == 1 and cells[0].col_span >= grid.n_cols - 1
            and _is_header_row(grid, r, fills, tol))


def header_row_count(grid: Grid, fills, tol: float) -> int:
    n = 0
    while n < grid.n_rows and _is_header_row(grid, n, fills, tol) and not _is_group_row(grid, n, fills, tol):
        n += 1
    return n


def column_names(grid: Grid, header_rows: int) -> list[str]:
    names = []
    for c in range(grid.n_cols):
        parts: list[str] = []
        seen: set[int] = set()
        for r in range(header_rows):
            cell = grid.cell_at(r, c)
            if id(cell) not in seen and cell.text.strip():
                seen.add(id(cell))
                parts.append(" ".join(cell.text.split()))
        names.append(" ".join(parts))
    return names


def _rows_of_lines(lines: list[CellLine]) -> list[list[CellLine]]:
    """Group lines that share a visual row (their vertical extents overlap), each row left to right."""
    rows: list[list[CellLine]] = []
    for ln in sorted((x for x in lines if not x.rotated), key=lambda x: (-x.bbox[3], x.bbox[0])):
        if rows and ln.bbox[3] >= min(r.bbox[1] for r in rows[-1]) + 0.35 * (ln.bbox[3] - ln.bbox[1]) \
                and ln.bbox[1] <= max(r.bbox[3] for r in rows[-1]):
            rows[-1].append(ln)
        else:
            rows.append([ln])
    return [sorted(r, key=lambda x: x.bbox[0]) for r in rows]


def _two_column_kv(cell: Cell) -> list[KV] | None:
    """A cell whose labels and values sit in two columns without a rule between them (labels ending in ":" on
    the left, values on the right): a label may wrap over several lines and so may its value, and a value line
    may sit a little above or below its label. None when the cell is not laid out that way."""
    lines = [ln for ln in cell.lines if not ln.rotated and ln.text.strip()]
    labels = [ln for ln in lines if ln.text.rstrip().endswith(":")]
    if not labels:
        return None
    split = max(ln.bbox[2] for ln in labels)
    right = [ln for ln in lines if ln.bbox[0] > split]
    left = [ln for ln in lines if ln.bbox[0] <= split]
    if not right or any(ln.bbox[2] > split + 2.0 for ln in left) or any(_INLINE_KV.match(ln.text) for ln in left):
        return None
    if any(a.bbox[1] < b.bbox[3] and b.bbox[1] < a.bbox[3] for i, a in enumerate(labels) for b in labels[i + 1:]):
        return None                       # two labels on one line: label/value columns repeat across the cell
    blocks: list[list[CellLine]] = []
    cur: list[CellLine] = []
    for ln in sorted(left, key=lambda x: -x.bbox[3]):
        if cur and cur[-1].bbox[1] - ln.bbox[3] > 0.8 * (ln.bbox[3] - ln.bbox[1]):
            return None                   # a label's wrapped lines sit close together; a gap means other text
        cur.append(ln)
        if ln.text.rstrip().endswith(":"):
            blocks.append(cur)
            cur = []
    if cur or not blocks:                 # left text after the last label: not a clean two-column layout
        return None
    values: list[list[CellLine]] = [[] for _ in blocks]
    for ln in right:
        mid = (ln.bbox[1] + ln.bbox[3]) / 2

        def gap(block: list[CellLine], mid: float = mid) -> float:
            lo, hi = min(x.bbox[1] for x in block), max(x.bbox[3] for x in block)
            return 0.0 if lo <= mid <= hi else min(abs(mid - lo), abs(mid - hi))
        n = min(range(len(blocks)), key=lambda i: gap(blocks[i]))
        if gap(blocks[n]) > 1.5 * (ln.bbox[3] - ln.bbox[1]):
            return None                   # a value line no label explains: leave the cell to the general reading
        values[n].append(ln)
    out: list[KV] = []
    group: str | None = None
    group_line: CellLine | None = None
    for i, (block, vals) in enumerate(zip(blocks, values, strict=True)):
        label = " ".join(" ".join(x.text.split()) for x in block).rstrip(":").strip()
        if not vals:
            if all(x.bold for x in block):            # a bold label with no value heads the pairs below it
                group, group_line = label, block[-1]
            continue
        vals.sort(key=lambda x: (-x.bbox[3], x.bbox[0]))
        texts = [" ".join(x.text.split()) for x in vals]
        out.append(KV(label, " ".join(texts), group, cell, block + vals, [group_line] if group_line else [], line=i,
                      value_lines=texts if len(texts) > 1 else []))
    return out


def _continues(kv: KV, ln: CellLine) -> bool:
    """A short plain line just under a pair, starting where the pair starts, is the rest of its value."""
    last = kv.lines[-1]
    height = last.bbox[3] - last.bbox[1]
    return (not ln.bold and len(ln.text.split()) <= 6 and 0 <= last.bbox[1] - ln.bbox[3] <= 0.8 * height
            and abs(ln.bbox[0] - kv.lines[0].bbox[0]) <= 4.0)


def extract_kv(cell: Cell) -> list[KV]:
    """Label/value pairs inside one cell: labels and values in two columns, 'Label: value' lines, a label with its
    value to the right on the row, or a label with its value on the next row. A bold line standing alone is a
    group heading for the pairs after it."""
    two = _two_column_kv(cell)
    if two:
        return two
    out: list[KV] = []
    group: str | None = None
    group_line: CellLine | None = None
    rows = _rows_of_lines(cell.lines)
    i = 0
    while i < len(rows):
        row = rows[i]
        if len(row) == 1:
            ln = row[0]
            inline = _INLINE_KV.match(ln.text)
            if ln.bold and not inline:
                group, group_line = ln.text, ln
            elif inline:
                out.append(KV(inline["label"].strip(), inline["value"].strip(), group, cell, [ln],
                              [group_line] if group_line else [], line=i))
            elif ln.text.endswith(":") and i + 1 < len(rows) and len(rows[i + 1]) == 1 \
                    and not rows[i + 1][0].text.endswith(":") and not _INLINE_KV.match(rows[i + 1][0].text):
                out.append(KV(ln.text.rstrip(":").strip(), rows[i + 1][0].text, group, cell, [ln, rows[i + 1][0]],
                              [group_line] if group_line else [], line=i))
                i += 1
            elif out and _continues(out[-1], ln):        # "TMD4: Parking Management" over "24x7 hrs"
                kv = out[-1]
                kv.value_lines = kv.value_lines or [kv.value]
                kv.value_lines.append(" ".join(ln.text.split()))
                kv.value = " ".join(kv.value_lines)
                kv.lines.append(ln)
            i += 1
            continue
        j = 0
        while j < len(row):
            ln = row[j]
            inline = _INLINE_KV.match(ln.text)
            if ln.text.endswith(":") and j + 1 < len(row) and not row[j + 1].text.endswith(":"):
                out.append(KV(ln.text.rstrip(":").strip(), row[j + 1].text, group, cell, [ln, row[j + 1]],
                              [group_line] if group_line else [], line=i))
                j += 2
            elif inline:
                out.append(KV(inline["label"].strip(), inline["value"].strip(), group, cell, [ln],
                              [group_line] if group_line else [], line=i))
                j += 1
            else:
                j += 1
        i += 1
    return out


def _heading_cell(cells: list[Cell], r: int) -> Cell | None:
    """A row whose only cell (ignoring cells carried down from rows above) is a single line ending in ':' with
    nothing after it: a heading such as 'Stakeholders consulted:' for the rows below."""
    own = [c for c in cells if c.row == r and not c.empty]
    if len(own) != 1:
        return None
    lines = [ln for ln in own[0].lines if ln.text.strip()]
    return own[0] if len(lines) == 1 and lines[0].text.rstrip().endswith(":") else None


def grid_kv(grid: Grid, fills, tol: float) -> list[KV]:
    """Label/value pairs across cells: a cell ending in ":" followed by a value cell in the same row, grouped
    under the header-fill row or the lone 'Heading:' row above them. Pairs inside a single cell are added for the
    cells left over, under the same group as their row. A cell spanning more rows than its label is not its value."""
    out: list[KV] = []
    used: set[int] = set()
    group: str | None = None
    group_cell: Cell | None = None
    row_group: dict[int, tuple[str | None, Cell | None]] = {}
    for r in range(grid.n_rows):
        cells = _unique(grid.row_slots(r))
        heading = None if _is_group_row(grid, r, fills, tol) else _heading_cell(cells, r)
        if _is_group_row(grid, r, fills, tol) or heading is not None:
            head = cells[0] if heading is None else heading
            group, group_cell = " ".join(head.text.split()).rstrip(":").strip() or group, head
            used.add(id(head))
            row_group[r] = (group, group_cell)
            continue
        row_group[r] = (group, group_cell)
        for i, cell in enumerate(cells):
            nxt = cells[i + 1] if i + 1 < len(cells) else None
            text = " ".join(cell.text.split())
            if text.endswith(":") and nxt is not None and not nxt.empty and not nxt.text.strip().endswith(":") \
                    and nxt.row_span <= cell.row_span:
                out.append(KV(text.rstrip(":").strip(), " ".join(nxt.text.split()), group, cell, cell.lines + nxt.lines,
                              group_cell.lines if group_cell else []))
                used.update({id(cell), id(nxt)})
    for cell in sorted(grid.cells, key=lambda c: (c.row, c.col)):
        if id(cell) in used or cell.empty:
            continue
        g, gc = row_group.get(cell.row, (None, None))
        for kv in extract_kv(cell):
            if kv.group is None and cell.row_span == 1 and g is not None:
                kv.group, kv.group_lines = g, (gc.lines if gc else [])
            out.append(kv)
    return out


def _value_for(text: str, fd: FieldDef | None) -> tuple[object, str | None, list[str], bool]:
    """(typed value, problem, notes, refers_elsewhere) for one cell's text."""
    raw = " ".join(text.split())
    key = normalise(raw)
    if key.startswith(REFERS_PREFIXES):                 # "See Above", "See above TTM stages"
        return None, None, ["refers elsewhere (resolved in a later phase)"], True
    if key in NOT_APPLICABLE:
        return None, None, ["not applicable or not stated"], False
    if fd is None:
        return raw, None, [], False
    value, problem = parse_value(raw, fd.type, fd.unit)
    return value, problem, [], False


class TableHandler(BaseHandler):
    route = Route.TABLE
    version = "1.0.0"

    def __init__(self) -> None:
        self._tables: list[TableInfo] = []

    def begin(self, ctx: HandlerContext) -> None:
        self._tables = []

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        info = self._analyse(ctx, element)
        self._tables.append(info)
        return HandlerResult()

    # --- analysis ------------------------------------------------------------------------------------------------

    def _analyse(self, ctx: HandlerContext, el: Element) -> TableInfo:
        t = ctx.cfg.tables
        kids = [k for k in ctx.store.on_page(el.page) if k.parent_id == el.element_id]
        grid = build_grid(el, [k for k in kids if k.type == ElementType.TEXT_BLOCK], t)
        header_rows = header_row_count(grid, t.header_fills, t.header_fill_tol)
        headers = column_names(grid, header_rows)
        n_boxes = sum(1 for k in kids if k.type == ElementType.CHECKBOX)
        non_empty = [c for c in grid.cells if not c.empty]
        h = ctx.store.pages[el.page].height_pt
        pg = ctx.cfg.page
        if kids and all(k.metadata.get("band") in ("header", "footer") for k in kids if k.type == ElementType.TEXT_BLOCK) \
                or in_band(el.bbox_pt, h, pg.header_band_pt, pg.footer_band_pt) is not None:
            shape = "header_block"
        elif n_boxes and len(non_empty) <= 3:
            shape = "checkbox_row"
        elif header_rows and grid.n_rows > header_rows:
            shape = "records_table"
        else:
            shape = "unknown"
        info = TableInfo(el, grid, kids, header_rows, headers, shape)
        if shape == "unknown":
            info.kv = grid_kv(grid, t.header_fills, t.header_fill_tol)
            if len(info.kv) >= 2:
                info.shape = "label_value_grid"
        return info

    # --- stitching -----------------------------------------------------------------------------------------------

    def _stitch(self, ctx: HandlerContext) -> None:
        tol = ctx.cfg.tables.stitch_x_tol_pt
        ordered = sorted(self._tables, key=lambda t: (t.element.page, -t.element.bbox_pt[3]))
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            if cur.header_rows or cur.shape not in ("unknown", "label_value_grid") or cur.element.page != prev.element.page + 1:
                continue
            base = (prev.continues or prev) if prev.shape == "records_table" else None      # always the first table
            if base is None or base.grid.n_cols != cur.grid.n_cols:
                continue
            if all(abs(a - b) <= tol for a, b in zip(base.grid.xs, cur.grid.xs, strict=True)):
                cur.shape, cur.continues, cur.header_rows, cur.headers, cur.kv = "records_table", base, 0, base.headers, []

    # --- records -------------------------------------------------------------------------------------------------

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        self._stitch(ctx)
        stamp = ctx.stamp(self, prompts.PROMPT_VERSIONS)
        result = HandlerResult()
        matches: dict[int, EntityMatch | None] = {}
        for info in sorted(self._tables, key=lambda t: (t.element.page, -t.element.bbox_pt[3])):
            base = info.continues or info
            if info.shape == "records_table":
                key = id(base)
                if key not in matches:
                    matches[key] = self._match(ctx, base)
                self._rows(ctx, info, base, matches[key], stamp, result)
            elif info.shape == "label_value_grid":
                self._label_values(ctx, info, stamp, result)
            result.records.append(self._table_record(ctx, info, matches.get(id(info.continues or info)), stamp))
            self._struck(info, result)
        lg.log_event("tables", "done", tables=len(self._tables), records=len(result.records))
        return result

    def _match(self, ctx: HandlerContext, info: TableInfo) -> EntityMatch | None:
        names = [n for n in info.headers if n]
        m = ctx.catalog.match_entity(names)
        if m is not None and not m.unmapped:
            return m
        if ctx.model is None or not names:
            return m
        samples = [info.grid.cell_at(r, c).text for r in range(info.header_rows, min(info.grid.n_rows, info.header_rows + 1))
                   for c in range(info.grid.n_cols)][:len(names)]
        answer = ask(ctx.model, prompts.x2_match_names(
            prompts.catalog_text(ctx.catalog.entities), "", "", "", "records_table", names, samples))
        nm = check_x2(answer, ctx.catalog, names)
        if nm is None or nm.entity == "unknown":
            return m
        mapped = dict(m.mapped) if m and m.entity == nm.entity else {}
        low = {n for n, (_, confident) in nm.mapped.items() if not confident}
        for n, (f, _) in nm.mapped.items():
            mapped.setdefault(n, f)
        out = EntityMatch(nm.entity, mapped, [n for n in names if n not in mapped], len(mapped) / len(names))
        info.element.metadata["x2_low_confidence"] = sorted(low)
        return out

    def _row_fields(self, ctx: HandlerContext, info: TableInfo, base: TableInfo, match: EntityMatch | None, r: int,
                    group: str | None) -> tuple[dict[str, FieldValue], list[Cell], list[Flag], dict, dict[str, int]]:
        fields: dict[str, FieldValue] = {}
        colmap: dict[str, int] = {}
        cells: list[Cell] = []
        flags: list[Flag] = []
        other: dict[str, str] = {}
        low = set(base.element.metadata.get("x2_low_confidence", []))
        for c, name in enumerate(base.headers):
            cell = info.grid.cell_at(r, c)
            if not name or (cell in cells):
                continue
            cells.append(cell)
            fname = match.mapped.get(name) if match else None
            fd = ctx.catalog.entities[match.entity].fields.get(fname) if match and fname else None
            text = cell.text_for_row(r)
            value, problem, notes, refers = _value_for(text, fd)
            raw = " ".join(text.split()) or None
            status = Status.VERIFIED
            if problem:
                status, notes = Status.UNVERIFIED, [*notes, problem]
            if name in low:
                status, notes = Status.UNVERIFIED, [*notes, "X2 mapped this name with low confidence"]
            if cell.row_span > 1:
                notes = [*notes, "merged"]
                flags.append(Flag.MERGED)
            if refers:
                flags.append(Flag.REFERS_ELSEWHERE)
            key = fname or snake(name)
            if fname is None:
                other[key] = name
            fields[key] = FieldValue(value if raw else None, raw=raw, unit=fd.unit if fd else None, status=status,
                                     method="catalog" if fname else "code", notes=notes)
            colmap[key] = c
        if group:
            fields["group"] = FieldValue(group, raw=group)
        return fields, cells, flags, other, colmap

    def _checks(self, fields: dict[str, FieldValue]) -> None:
        """Value checks: flag, never fix. Start RP before end RP; percentages 0-100; speeds in tens."""
        s, e = fields.get("start_rp"), fields.get("end_rp")
        if s and e and isinstance(s.value, int | float) and isinstance(e.value, int | float) and s.value >= e.value:
            for fv in (s, e):
                fv.status = Status.UNVERIFIED
                fv.notes.append("start RP is not below end RP")
        for name, fv in fields.items():
            if name.endswith("_pct") and isinstance(fv.value, int | float) and not 0 <= fv.value <= 100:
                fv.status = Status.UNVERIFIED
                fv.notes.append("percentage outside 0-100")
            if name == "posted_speed_kmh" and isinstance(fv.value, int) and fv.value % 10:
                fv.status = Status.UNVERIFIED
                fv.notes.append("speed is not a multiple of 10")

    def _rows(self, ctx: HandlerContext, info: TableInfo, base: TableInfo, match: EntityMatch | None, stamp,
              result: HandlerResult) -> None:
        grid = info.grid
        group: str | None = base.element.metadata.get("group")
        prev: Record | None = base.rows[-1] if info.continues and base.rows else None
        for r in range(info.header_rows, grid.n_rows):
            cells0 = _unique(grid.row_slots(r))
            if cells0 and all(c.fill is not None and c.col_span >= grid.n_cols - 1 for c in cells0) and cells0[0].row == r:
                group = " ".join(cells0[0].text.split()) or group       # a header-fill row labels the rows beneath
                continue
            if all(c.empty for c in cells0):
                continue
            if prev is not None and info.continues and r == info.header_rows and grid.cell_at(r, 0).empty:
                self._append_continuation(info, base, prev, r)           # first cell empty after a page break
                prev = None
                continue
            fields, cells, flags, other, colmap = self._row_fields(ctx, info, base, match, r, group)
            self._checks(fields)
            text = " ".join(c.text_for_row(r) for c in cells)
            if _EMAIL.search(text) or any(n.kind == "phone" for n in extract(text)):
                flags.append(Flag.CONTACT_INFO)
            row_lines = [ln for c in cells for ln in c.lines_for_row(r)]
            struck = bool(row_lines) and all(ln.struck for ln in row_lines)
            ev = [Evidence.of(info.element)] + [Evidence.of(e) for c in cells for e in c.elements_for_row(r)]
            rec = Record(match.entity if match else "table_row", info.element.page, stamp, fields,
                         match.entity if match else None, ev, flags=list(dict.fromkeys(flags)),
                         status=Status.NOT_APPLICABLE if struck else Status.VERIFIED,
                         applicability="not_applicable" if struck else "active",
                         data={"table": info.element.element_id, "row": r, "headers": base.headers, "other_fields": other,
                               "colmap": colmap,
                               "cells": [{"row": c.row, "col": c.col, "row_span": c.row_span, "col_span": c.col_span,
                                          "text": " ".join(c.text_for_row(r).split())} for c in cells]})
            result.records.append(rec)
            base.rows.append(rec)
            prev = rec

    def _append_continuation(self, info: TableInfo, base: TableInfo, prev: Record, r: int) -> None:
        """A row continued over a page break: its cell texts are added to the previous row's values."""
        for key, col in prev.data["colmap"].items():
            cell = info.grid.cell_at(r, col)
            if not cell.lines_for_row(r):
                continue
            text = " ".join(cell.text_for_row(r).split())
            fv = prev.fields[key]
            fv.raw = f"{fv.raw or ''} {text}".strip()
            if isinstance(fv.value, str) or fv.value is None:
                fv.value = fv.raw
            fv.notes.append(f"continued on page {info.element.page}")
            prev.evidence += [Evidence.of(e) for e in cell.elements_for_row(r)]
        prev.data.setdefault("continued_on", []).append(info.element.page)

    def _label_values(self, ctx: HandlerContext, info: TableInfo, stamp, result: HandlerResult) -> None:
        labels = [kv.label for kv in info.kv]
        match = ctx.catalog.match_entity(labels)
        if match is None or len(match.mapped) < 2:
            return                                    # no entity: the table record keeps the raw pairs
        groups: dict[str | None, list[KV]] = {}
        for kv in info.kv:
            groups.setdefault(kv.group, []).append(kv)
        for group, items in groups.items():
            fields: dict[str, FieldValue] = {}
            ev = [Evidence.of(info.element)]
            flags: list[Flag] = []
            for kv in items:
                fname = match.mapped.get(kv.label)
                fd = ctx.catalog.entities[match.entity].fields.get(fname) if fname else None
                if fname is None or fname in fields or (fd is not None and fd.type == "bool"):
                    continue                      # ticked/unticked answers belong to the checkbox handler
                value, problem, notes, refers = _value_for(kv.value, fd)
                status = Status.UNVERIFIED if problem else Status.VERIFIED
                fields[fname] = FieldValue(value, raw=kv.value, unit=fd.unit if fd else None, status=status,
                                           method="catalog", notes=[*notes, *([problem] if problem else [])])
                if refers:
                    flags.append(Flag.REFERS_ELSEWHERE)
                ev += [Evidence.of(e, kv.value) for e in {ln.element.element_id: ln.element for ln in kv.lines}.values()]
                ev += [Evidence.of(e) for e in {ln.element.element_id: ln.element for ln in kv.group_lines}.values()]
            if not fields:
                continue
            day = None
            if group:
                m = _PARENS.search(group)
                day = re.sub(r"^during\s+", "", (m.group(1) if m else group).strip(), flags=re.I)
                if "day_type" in ctx.catalog.entities[match.entity].fields or match.entity == "shift_window":
                    fields["day_type"] = FieldValue(day, raw=group)
            self._checks(fields)
            result.records.append(Record(match.entity, info.element.page, stamp, fields, match.entity, ev,
                                         flags=list(dict.fromkeys(flags)), data={"table": info.element.element_id, "group": group,
                                                            "shape": "label_value_grid"}))

    def _table_record(self, ctx: HandlerContext, info: TableInfo, match: EntityMatch | None, stamp) -> Record:
        grid = info.grid
        return Record("table", info.element.page, stamp, {}, match.entity if match else None,
                      [Evidence.of(info.element)],
                      data={"table": info.element.element_id, "shape": info.shape, "n_rows": grid.n_rows,
                            "n_cols": grid.n_cols, "header_rows": info.header_rows, "headers": info.headers,
                            "continues": info.continues.element.element_id if info.continues else None,
                            "pairs": [{"label": kv.label, "value": kv.value, "group": kv.group, "row": kv.cell.row,
                                       "line": kv.line, **({"value_lines": kv.value_lines} if kv.value_lines else {}),
                                       "elements": sorted({ln.element.element_id for ln in kv.lines + kv.group_lines})}
                                      for kv in info.kv],
                            "cells": [] if info.shape in ("records_table", "header_block") else [
                                {"row": c.row, "col": c.col, "row_span": c.row_span, "col_span": c.col_span,
                                 "text": " ".join(c.text.split())} for c in grid.cells if not c.empty]})

    def _struck(self, info: TableInfo, result: HandlerResult) -> None:
        lines = [ln for c in info.grid.cells for ln in c.lines]
        by_block: dict[str, list[bool]] = {}
        for ln in lines:
            by_block.setdefault(ln.element.element_id, []).append(ln.struck)
        for eid, flags in by_block.items():
            if flags and all(flags):
                result.element_updates[eid] = ElementUpdate(add_flags=(Flag.STRUCK,))
