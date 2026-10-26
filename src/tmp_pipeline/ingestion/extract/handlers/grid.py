"""Cell grid of a table region (plan section 2.2): from the ruling segments Phase 1 kept, build slots between
the rules, join slots whose shared edge has no rule (merged cells), and put every text line in the cell that
holds its centre. Text is placed per line, not per block, because Phase 1 can put the header word and the
cell text under it in one block when nothing but a rule separates them."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from ....config import TablesCfg
from ...geometry import centre
from ...models import Element
from ...vocab import ElementType
from .textutil import join_lines


@dataclass
class CellLine:
    element: Element
    text: str
    bbox: list[float]
    struck: bool
    bold: bool
    size: float
    rotated: bool


@dataclass
class Cell:
    row: int
    col: int
    row_span: int
    col_span: int
    bbox: list[float]
    lines: list[CellLine] = field(default_factory=list)
    fill: tuple[float, float, float] | None = None
    row_bands: list[tuple[float, float]] = field(default_factory=list)   # (top, bottom) of each spanned row

    @staticmethod
    def _ordered(lines: list[CellLine]) -> list[CellLine]:
        return lines if any(ln.rotated for ln in lines) else sorted(lines, key=lambda ln: (-round(ln.bbox[3]), ln.bbox[0]))

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self._ordered(self.lines)])

    def lines_for_row(self, row: int) -> list[CellLine]:
        """The lines that belong to one row of a cell that spans several.

        When the cell's text falls in two or more of its row bands the cell is a stack of per-row values (three
        hazards sharing one tall cell), so each row gets the lines inside its band. When all the text sits in one
        band it is one value shared by every row it covers (a speed sign spanning two roads)."""
        if self.row_span == 1 or not self.row_bands:
            return self.lines
        by_band: dict[int, list[CellLine]] = {}
        for ln in self.lines:
            cy = (ln.bbox[1] + ln.bbox[3]) / 2
            for i, (top, bottom) in enumerate(self.row_bands):
                if bottom <= cy <= top:
                    by_band.setdefault(i, []).append(ln)
                    break
        if len(by_band) < 2:
            return self.lines
        return by_band.get(row - self.row, [])

    def text_for_row(self, row: int) -> str:
        return join_lines([ln.text for ln in self._ordered(self.lines_for_row(row))])

    def elements_for_row(self, row: int) -> list[Element]:
        return list({ln.element.element_id: ln.element for ln in self.lines_for_row(row)}.values())

    @property
    def empty(self) -> bool:
        return not self.lines

    @property
    def struck(self) -> bool:
        return bool(self.lines) and all(ln.struck for ln in self.lines)

    def elements(self) -> list[Element]:
        return list({ln.element.element_id: ln.element for ln in self.lines}.values())


@dataclass
class Grid:
    xs: list[float]                   # column edges, ascending
    ys: list[float]                   # row edges, top first (descending y)
    cells: list[Cell]
    slots: dict[tuple[int, int], Cell]

    @property
    def n_rows(self) -> int:
        return len(self.ys) - 1

    @property
    def n_cols(self) -> int:
        return len(self.xs) - 1

    def cell_at(self, row: int, col: int) -> Cell:
        return self.slots[(row, col)]

    def row_slots(self, row: int) -> list[Cell]:
        """One entry per column; a cell that spans columns or rows appears in each slot it covers."""
        return [self.slots[(row, c)] for c in range(self.n_cols)]


def _edges(positions: list[float], lo: float, hi: float, tol: float) -> list[float]:
    """Cluster rule positions, making sure the region's own boundary is an edge."""
    groups: list[list[float]] = []
    for v in sorted([*positions, lo, hi]):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [round(sum(g) / len(g), 2) for g in groups]


def _covered(rules: list[list[float]], pos: float, a: float, b: float, tol: float, cover: float) -> bool:
    """Do the rules lying at ``pos`` together cover at least ``cover`` of the edge from a to b?"""
    total = b - a
    if total <= 0:
        return True
    segs = sorted((max(lo, a), min(hi, b)) for p, lo, hi in rules if abs(p - pos) <= tol and hi > a and lo < b)
    covered, end = 0.0, a
    for lo, hi in segs:
        lo = max(lo, end)
        if hi > lo:
            covered += hi - lo
            end = hi
    return covered >= cover * total


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def build_grid(table: Element, children: list[Element], cfg: TablesCfg) -> Grid:
    x0, y0, x1, y1 = table.bbox_pt
    h_rules, v_rules = table.metadata.get("h_rules", []), table.metadata.get("v_rules", [])
    tol = cfg.edge_tol_pt
    xs = _edges([v[0] for v in v_rules], x0, x1, tol)
    ys = sorted(_edges([h[0] for h in h_rules], y0, y1, tol), reverse=True)
    nr, nc = len(ys) - 1, len(xs) - 1
    idx = lambda r, c: r * nc + c    # noqa: E731
    parent = list(range(nr * nc))
    for r in range(nr):
        for c in range(nc):
            if r + 1 < nr and not _covered(h_rules, ys[r + 1], xs[c], xs[c + 1], tol, cfg.rule_cover):
                parent[_find(parent, idx(r, c))] = _find(parent, idx(r + 1, c))
            if c + 1 < nc and not _covered(v_rules, xs[c + 1], ys[r + 1], ys[r], tol, cfg.rule_cover):
                parent[_find(parent, idx(r, c))] = _find(parent, idx(r, c + 1))
    comps: dict[int, list[tuple[int, int]]] = {}
    for r in range(nr):
        for c in range(nc):
            comps.setdefault(_find(parent, idx(r, c)), []).append((r, c))
    cells: list[Cell] = []
    slots: dict[tuple[int, int], Cell] = {}
    for members in comps.values():
        rs, cs = [m[0] for m in members], [m[1] for m in members]
        r0, r1, c0, c1 = min(rs), max(rs), min(cs), max(cs)
        cell = Cell(r0, c0, r1 - r0 + 1, c1 - c0 + 1, [xs[c0], ys[r1 + 1], xs[c1 + 1], ys[r0]],
                    row_bands=[(ys[r], ys[r + 1]) for r in range(r0, r1 + 1)])
        cells.append(cell)
        for r in range(r0, r1 + 1):             # a non-rectangular component is covered by its bounding box
            for c in range(c0, c1 + 1):
                slots[(r, c)] = cell
    _split_filled_tiles(cells, slots, xs, table.metadata.get("fills", []))
    cells.sort(key=lambda c: (c.row, c.col))
    _assign_fills(cells, table.metadata.get("fills", []))
    neg_ys = [-y for y in ys]

    def locate(px: float, py: float) -> Cell:
        c = min(max(bisect_right(xs, px) - 1, 0), nc - 1)
        r = min(max(bisect_right(neg_ys, -py) - 1, 0), nr - 1)
        return slots[(r, c)]
    for blk in children:
        if blk.type != ElementType.TEXT_BLOCK:
            continue
        rotated = blk.metadata.get("rotation", 0) not in (0, 180)
        for ln in blk.representations.get("lines", []):
            if not ln["text"].strip():
                continue
            cx, cy = centre(ln["bbox"])
            locate(cx, cy).lines.append(CellLine(blk, ln["text"].strip(), ln["bbox"], bool(ln.get("struck")),
                                                 bool(ln["bold"]), float(ln["size"]), rotated))
    return Grid(xs, ys, cells, slots)


def _assign_fills(cells: list[Cell], fills: list[dict]) -> None:
    """A cell's fill is the largest filled rect that covers most of it (small badges inside are not the cell's)."""
    for cell in cells:
        area = (cell.bbox[2] - cell.bbox[0]) * (cell.bbox[3] - cell.bbox[1])
        best, best_area = None, 0.0
        for f in fills:
            b = f["bbox"]
            ix = max(0.0, min(b[2], cell.bbox[2]) - max(b[0], cell.bbox[0]))
            iy = max(0.0, min(b[3], cell.bbox[3]) - max(b[1], cell.bbox[1]))
            if ix * iy > best_area and ix * iy >= 0.6 * area:
                best, best_area = f, ix * iy
        if best is not None:
            cell.fill = (best["rgb"][0], best["rgb"][1], best["rgb"][2])


def _split_filled_tiles(cells: list[Cell], slots: dict[tuple[int, int], Cell], xs: list[float],
                        fills: list[dict]) -> None:
    """A header drawn as separate filled boxes with no rules between them reads as one wide cell. When two or
    more full-height fills tile a cell that spans columns, split it along them. Small badges inside a cell are
    shorter than the cell and never split it."""
    for cell in list(cells):
        if cell.col_span < 2:
            continue
        h = cell.bbox[3] - cell.bbox[1]
        tiles = sorted((f["bbox"] for f in fills
                        if cell.bbox[0] - 1 <= f["bbox"][0] and f["bbox"][2] <= cell.bbox[2] + 1
                        and f["bbox"][3] - f["bbox"][1] >= 0.9 * h and cell.bbox[1] - 1 <= f["bbox"][1]
                        and f["bbox"][3] <= cell.bbox[3] + 1 and f["bbox"][2] - f["bbox"][0] >= 8),
                       key=lambda b: b[0])
        if len(tiles) < 2 or any(b[0] < a[2] - 1 for a, b in zip(tiles, tiles[1:], strict=False)):
            continue
        if sum(t[2] - t[0] for t in tiles) < 0.85 * (cell.bbox[2] - cell.bbox[0]):
            continue
        cells.remove(cell)
        for t in tiles:
            cols = [c for c in range(cell.col, cell.col + cell.col_span) if t[0] - 1 <= (xs[c] + xs[c + 1]) / 2 <= t[2] + 1]
            if not cols:
                continue
            tile = Cell(cell.row, cols[0], cell.row_span, len(cols), [t[0], cell.bbox[1], t[2], cell.bbox[3]],
                        row_bands=list(cell.row_bands))
            cells.append(tile)
            for r in range(cell.row, cell.row + cell.row_span):
                for c in cols:
                    slots[(r, c)] = tile
