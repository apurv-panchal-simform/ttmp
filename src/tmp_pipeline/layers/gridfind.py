"""Table regions from ruling lines and fills (plan section 2.2, coarse): the outermost connected grid.

Rulings are lines and thin rects; fills are non-white filled rects; stroked boxes count as cells.
Connected parts (touching within a tolerance) form one region; nested colour badges are absorbed
because they sit inside the region's box. Cells, rows and headers are Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass

from .reader import PageLayers, Shape

_WHITE = (0.97, 0.97, 0.97)


@dataclass
class GridRegion:
    bbox: list[float]
    n_xs: int
    n_ys: int
    n_parts: int
    n_fills: int
    rating_cells: int = 0


def _is_white(c) -> bool:
    return c is not None and all(v >= _WHITE[0] for v in c)


def _parts(page: PageLayers, cfg: dict) -> list[list[float]]:
    t = cfg["tables"]
    page_area = page.width * page.height
    parts: list[list[float]] = []
    for s in page.lines:
        parts.append([s.x0, s.y0, s.x1, s.y1])
    for r in page.rects:
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if w * h > 0.8 * page_area:
            continue                                   # page background
        thin = min(w, h) < t["thin_pt"]
        if thin:
            if max(w, h) >= t["min_rule_len_pt"]:
                parts.append([r.x0, r.y0, r.x1, r.y1])
        elif min(w, h) >= t["min_fill_pt"]:
            if r.fill and not _is_white(r.fill_colour) or (r.stroke and not r.fill) or (r.fill and r.stroke):
                parts.append([r.x0, r.y0, r.x1, r.y1])
    return parts


def _cluster(values: list[float], tol: float) -> int:
    values = sorted(values)
    n, last = 0, None
    for v in values:
        if last is None or v - last > tol:
            n += 1
        last = v
    return n


def detect_tables(page: PageLayers, cfg: dict) -> list[GridRegion]:
    t = cfg["tables"]
    parts = _parts(page, cfg)
    n = len(parts)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tol = t["join_tol_pt"]
    order = sorted(range(n), key=lambda i: parts[i][0])
    active: list[int] = []
    for i in order:
        a = parts[i]
        active = [j for j in active if parts[j][2] + tol >= a[0]]
        for j in active:
            b = parts[j]
            if a[0] <= b[2] + tol and b[0] <= a[2] + tol and a[1] <= b[3] + tol and b[1] <= a[3] + tol:
                parent[find(i)] = find(j)
        active.append(i)
    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)

    tables: list[GridRegion] = []
    colours = [tuple(c) for c in cfg["zones"]["rating_colours"]]
    rtol = cfg["zones"]["rating_tol"]
    for members in comps.values():
        ps = [parts[i] for i in members]
        if len(ps) < t["min_parts"]:
            continue
        xs = [p[0] for p in ps] + [p[2] for p in ps]
        ys = [p[1] for p in ps] + [p[3] for p in ps]
        nx, ny = _cluster(xs, t["edge_tol_pt"]), _cluster(ys, t["edge_tol_pt"])
        if nx < 2 or ny < 2 or (nx - 1) * (ny - 1) < t["min_cells"]:
            continue
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        tables.append(GridRegion(bbox, nx, ny, len(ps), 0))
    # fills and rating-colour cells inside each region
    for r in page.rects:
        if not (r.fill and r.fill_colour):
            continue
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if min(w, h) < t["min_fill_pt"]:
            continue
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        inside = [g for g in tables if g.bbox[0] <= cx <= g.bbox[2] and g.bbox[1] <= cy <= g.bbox[3]]
        if not inside:
            continue
        g = min(inside, key=lambda g: (g.bbox[2] - g.bbox[0]) * (g.bbox[3] - g.bbox[1]))
        if not _is_white(r.fill_colour):
            g.n_fills += 1
        if any(all(abs(a - b) < rtol for a, b in zip(r.fill_colour, c)) for c in colours):
            g.rating_cells += 1
    return _drop_nested(tables)


def _drop_nested(tables: list[GridRegion]) -> list[GridRegion]:
    """Keep the outermost region when one box sits wholly inside another."""
    keep = []
    for i, a in enumerate(tables):
        nested = any(
            j != i and b.bbox[0] <= a.bbox[0] and b.bbox[1] <= a.bbox[1] and b.bbox[2] >= a.bbox[2] and b.bbox[3] >= a.bbox[3]
            and (b.bbox != a.bbox or j < i)
            for j, b in enumerate(tables))
        if not nested:
            keep.append(a)
    return keep
