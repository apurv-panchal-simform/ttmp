"""Table regions from ruling lines and fills (plan section 2.2, coarse): the outermost connected grid.

Rulings are lines and thin rects; fills are non-white filled rects; stroked boxes count as cells.
Connected parts (touching within a tolerance) form one region; nested colour badges are absorbed
because they sit inside the region's box. Cells, rows and headers are Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ....config import Config
from .reader import PageLayers

_WHITE = (0.97, 0.97, 0.97)


@dataclass
class GridRegion:
    bbox: list[float]
    n_xs: int
    n_ys: int
    n_parts: int
    n_fills: int
    rating_cells: int = 0
    h_rules: list[list[float]] = field(default_factory=list)   # merged horizontal ruling segments [y, x0, x1]
    v_rules: list[list[float]] = field(default_factory=list)   # merged vertical ruling segments [x, y0, y1]
    fills: list[dict] = field(default_factory=list)        # non-white filled cells: {"bbox": [...], "rgb": [...]}


def _is_white(c) -> bool:
    return c is not None and all(v >= _WHITE[0] for v in c)


def _box_borders(b: list[float]) -> list[list[float]]:
    """A cell drawn as a stroked box contributes its four sides as thin rules."""
    return [[b[0], b[1], b[2], b[1]], [b[0], b[3], b[2], b[3]], [b[0], b[1], b[0], b[3]], [b[2], b[1], b[2], b[3]]]


def _parts(page: PageLayers, cfg: Config) -> tuple[list[list[float]], list[list[list[float]]]]:
    """Table parts and, per part, the ruling segments it contributes (a rule is itself; a stroked box its sides)."""
    t = cfg.tables
    page_area = page.width * page.height
    parts: list[list[float]] = []
    rules: list[list[list[float]]] = []
    for s in page.lines:
        parts.append([s.x0, s.y0, s.x1, s.y1])
        rules.append([[s.x0, s.y0, s.x1, s.y1]])
    for r in page.rects:
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if w * h > cfg.page.background_share * page_area:
            continue                                   # page background
        thin = min(w, h) < t.thin_pt
        if thin:
            if max(w, h) >= t.min_rule_len_pt:
                parts.append([r.x0, r.y0, r.x1, r.y1])
                rules.append([[r.x0, r.y0, r.x1, r.y1]])
        elif min(w, h) >= t.min_fill_pt:
            if r.fill and not _is_white(r.fill_colour) or (r.stroke and not r.fill) or (r.fill and r.stroke):
                parts.append([r.x0, r.y0, r.x1, r.y1])
                rules.append(_box_borders([r.x0, r.y0, r.x1, r.y1]) if r.stroke else [])
    return parts, rules


def _merge_rules(segs: list[list[float]], cfg: Config) -> tuple[list[list[float]], list[list[float]]]:
    """Horizontal [y, x0, x1] and vertical [x, y0, y1] ruling segments, collinear touching pieces joined."""
    t = cfg.tables
    hs: list[list[float]] = []
    vs: list[list[float]] = []
    for x0, y0, x1, y1 in segs:
        if abs(y1 - y0) <= abs(x1 - x0):
            hs.append([(y0 + y1) / 2, min(x0, x1), max(x0, x1)])
        else:
            vs.append([(x0 + x1) / 2, min(y0, y1), max(y0, y1)])

    def join(items: list[list[float]]) -> list[list[float]]:
        # 1-D clustering on position (never by rounding, which splits values that sit on a rounding boundary),
        # then touching pieces within a cluster are joined along the rule
        clusters: list[list[list[float]]] = []
        for item in sorted(items):
            if clusters and item[0] - clusters[-1][-1][0] <= t.edge_tol_pt:
                clusters[-1].append(item)
            else:
                clusters.append([item])
        out: list[list[float]] = []
        for cluster in clusters:
            seg: list[float] | None = None
            for pos, a, b in sorted(cluster, key=lambda i: i[1]):
                if seg is not None and a <= seg[2] + t.join_tol_pt:
                    seg[2] = max(seg[2], b)
                else:
                    seg = [pos, a, b]
                    out.append(seg)
        return [[round(p, 2), round(a, 2), round(b, 2)] for p, a, b in out]
    return join(hs), join(vs)


def _clusters(values: list[float], tol: float) -> list[float]:
    """Mean of each run of values closer together than ``tol``, ascending."""
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [round(sum(g) / len(g), 2) for g in groups]


def detect_tables(page: PageLayers, cfg: Config) -> list[GridRegion]:
    t = cfg.tables
    parts, part_rules = _parts(page, cfg)
    n = len(parts)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tol = t.join_tol_pt
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
    colours = [tuple(c) for c in cfg.zones.rating_colours]
    rtol = cfg.zones.rating_tol
    for members in comps.values():
        ps = [parts[i] for i in members]
        segs = [seg for i in members for seg in part_rules[i]]
        if len(ps) < t.min_parts:
            continue
        xs = [p[0] for p in ps] + [p[2] for p in ps]
        ys = [p[1] for p in ps] + [p[3] for p in ps]
        nx, ny = len(_clusters(xs, t.edge_tol_pt)), len(_clusters(ys, t.edge_tol_pt))
        if nx < 2 or ny < 2 or (nx - 1) * (ny - 1) < t.min_cells:
            continue
        bbox = [min(xs), min(ys), max(xs), max(ys)]
        h_rules, v_rules = _merge_rules(segs, cfg)
        tables.append(GridRegion(bbox, nx, ny, len(ps), 0, h_rules=h_rules, v_rules=v_rules))
    # fills and rating-colour cells inside each region
    for r in page.rects:
        if not (r.fill and r.fill_colour):
            continue
        w, h = r.x1 - r.x0, r.y1 - r.y0
        if min(w, h) < t.min_fill_pt:
            continue
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        inside = [g for g in tables if g.bbox[0] <= cx <= g.bbox[2] and g.bbox[1] <= cy <= g.bbox[3]]
        if not inside:
            continue
        g = min(inside, key=lambda g: (g.bbox[2] - g.bbox[0]) * (g.bbox[3] - g.bbox[1]))
        if not _is_white(r.fill_colour):
            g.n_fills += 1
            g.fills.append({"bbox": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                            "rgb": [round(float(v), 3) for v in r.fill_colour[:3]]})
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
