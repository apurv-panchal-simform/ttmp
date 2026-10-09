"""Characters -> words -> lines -> segments -> blocks (plan section 2.1 reading steps, coarse form).

Lines split into segments at wide gaps; segments join into blocks by vertical adjacency; side-by-side
boxes are separate blocks read left to right. Rotated runs are rebuilt from their glyphs along their own
axis, so the default word order is never trusted for them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .reader import Char, PageLayers

# rotation -> (reading axis value, next-line axis value) as functions of a char box
_AXES = {
    0: (lambda c: (c.x0, c.x1), lambda c: -(c.y0 + c.y1) / 2),
    180: (lambda c: (-c.x1, -c.x0), lambda c: (c.y0 + c.y1) / 2),
    90: (lambda c: (c.y0, c.y1), lambda c: (c.x0 + c.x1) / 2),
    270: (lambda c: (-c.y1, -c.y0), lambda c: -(c.x0 + c.x1) / 2),
}


@dataclass
class Segment:
    text: str
    rotation: int
    size: float
    bold: bool
    bbox: list[float]
    lo: float            # extent along the reading axis
    hi: float
    ly: float            # position across lines (increases to the next line)
    band: str = "body"
    table: int | None = None


@dataclass
class TextBlock:
    text: str
    rotation: int
    size: float
    bold: bool
    bbox: list[float]
    band: str
    table: int | None


def _lines(chars: list[Char], rot: int) -> list[list[Char]]:
    pos, ly = _AXES[rot]
    chars = sorted(chars, key=lambda c: ly(c))
    lines: list[list[Char]] = []
    for c in chars:
        if lines and abs(ly(c) - ly(lines[-1][0])) < 0.6 * max(c.size, 1):
            lines[-1].append(c)
        else:
            lines.append([c])
    return [sorted(l, key=lambda c: pos(c)[0]) for l in lines]


def _vertical_rules(page: PageLayers, cfg: dict) -> list[tuple[float, float, float]]:
    """(x, y0, y1) of vertical ruling lines: cell borders must split text segments."""
    thin = cfg["tables"]["thin_pt"]
    rules = []
    for s in page.lines + page.rects:
        w, h = s.x1 - s.x0, s.y1 - s.y0
        if w < thin and h >= cfg["tables"]["min_rule_len_pt"]:
            rules.append(((s.x0 + s.x1) / 2, s.y0, s.y1))
    return rules


def _segments_for_line(line: list[Char], rot: int, cfg: dict, vrules=()) -> list[Segment]:
    pos, ly = _AXES[rot]
    t = cfg["text"]
    out: list[Segment] = []
    words: list[list[Char]] = []
    cur: list[Char] = []
    prev_hi = None
    for c in line:
        lo, hi = pos(c)
        if not c.text.strip():
            if cur:
                words.append(cur)
                cur = []
            prev_hi = hi
            continue
        if cur and prev_hi is not None and lo - prev_hi > t["word_gap_em"] * c.size:
            words.append(cur)
            cur = []
        cur.append(c)
        prev_hi = hi
    if cur:
        words.append(cur)
    # split words into segments at wide gaps
    seg_words: list[list[list[Char]]] = []
    for w in words:
        if seg_words:
            last = seg_words[-1][-1][-1]
            gap = pos(w[0])[0] - pos(last)[1]
            limit = max(t["segment_gap_em"] * w[0].size, t["segment_gap_min_pt"])
            a, b = pos(last)[1], pos(w[0])[0]
            ymid = (w[0].y0 + w[0].y1) / 2
            crosses = rot in (0, 180) and any(
                min(abs(a), abs(b)) - 0.5 <= abs(x) <= max(abs(a), abs(b)) + 0.5 and y0 <= ymid <= y1
                for x, y0, y1 in vrules) if vrules else False
            if gap <= limit and not crosses:
                seg_words[-1].append(w)
                continue
        seg_words.append([w])
    for ws in seg_words:
        cs = [c for w in ws for c in w]
        text = " ".join("".join(c.text for c in w) for w in ws)
        out.append(Segment(
            text=text, rotation=rot, size=sum(c.size for c in cs) / len(cs), bold=all(c.bold for c in cs),
            bbox=[min(c.x0 for c in cs), min(c.y0 for c in cs), max(c.x1 for c in cs), max(c.y1 for c in cs)],
            lo=min(pos(c)[0] for c in cs), hi=max(pos(c)[1] for c in cs), ly=sum(ly(c) for c in cs) / len(cs)))
    return out


def segments(page: PageLayers, cfg: dict) -> list[Segment]:
    by_rot: dict[int, list[Char]] = {}
    for c in page.chars:
        if c.text.strip() or c.text == " ":
            by_rot.setdefault(c.rotation if c.rotation in _AXES else 0, []).append(c)
    segs: list[Segment] = []
    vrules = _vertical_rules(page, cfg)
    for rot, chars in by_rot.items():
        for line in _lines(chars, rot):
            segs.extend(_segments_for_line(line, rot, cfg, vrules))
    hb, fb = cfg["page"]["header_band_pt"], cfg["page"]["footer_band_pt"]
    for s in segs:
        if s.rotation == 0:
            cy = (s.bbox[1] + s.bbox[3]) / 2
            s.band = "header" if cy > page.height - hb else ("footer" if cy < fb else "body")
    return segs


def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _follows(a: Segment, b: Segment, cfg: dict) -> bool:
    """b sits on the line after a, in the same column."""
    t = cfg["text"]
    gap = b.ly - a.ly
    if gap <= 0 or gap > (max(a.size, b.size) * (1.2 + t["line_join_em"])):
        return False
    overlap = min(a.hi, b.hi) - max(a.lo, b.lo)
    narrow = min(a.hi - a.lo, b.hi - b.lo)
    return overlap >= 0.5 * narrow or abs(a.lo - b.lo) <= t["left_align_tol_pt"]


def build_text_blocks(page: PageLayers, tables: list, cfg: dict) -> list[TextBlock]:
    segs = segments(page, cfg)
    for s in segs:
        cx, cy = (s.bbox[0] + s.bbox[2]) / 2, (s.bbox[1] + s.bbox[3]) / 2
        inside = [i for i, t in enumerate(tables) if t.bbox[0] <= cx <= t.bbox[2] and t.bbox[1] <= cy <= t.bbox[3]]
        if inside:
            s.table = min(inside, key=lambda i: (tables[i].bbox[2] - tables[i].bbox[0]) * (tables[i].bbox[3] - tables[i].bbox[1]))
    n = len(segs)
    parent = list(range(n))
    order = sorted(range(n), key=lambda i: (segs[i].rotation, segs[i].ly))
    for ai, i in enumerate(order):
        a = segs[i]
        for j in order[ai + 1:]:
            b = segs[j]
            if b.rotation != a.rotation:
                break
            if b.ly - a.ly > max(a.size, b.size) * 2.5:
                break
            if (a.band, a.table) == (b.band, b.table) and _follows(a, b, cfg):
                parent[_find(parent, j)] = _find(parent, i)
    groups: dict[int, list[Segment]] = {}
    for i in range(n):
        groups.setdefault(_find(parent, i), []).append(segs[i])
    blocks: list[TextBlock] = []
    for g in groups.values():
        g.sort(key=lambda s: (round(s.ly / 2), s.lo))
        lines: list[list[Segment]] = []
        for s in g:
            if lines and abs(s.ly - lines[-1][0].ly) < 0.6 * s.size:
                lines[-1].append(s)
            else:
                lines.append([s])
        text = "\n".join(" ".join(s.text for s in sorted(l, key=lambda s: s.lo)) for l in lines)
        blocks.append(TextBlock(
            text=text, rotation=g[0].rotation, size=sum(s.size for s in g) / len(g), bold=all(s.bold for s in g),
            bbox=[min(s.bbox[0] for s in g), min(s.bbox[1] for s in g), max(s.bbox[2] for s in g), max(s.bbox[3] for s in g)],
            band=g[0].band, table=g[0].table))
    return _reading_order(blocks, page, cfg)


def _reading_order(blocks: list[TextBlock], page: PageLayers, cfg: dict) -> list[TextBlock]:
    tol = cfg["text"]["row_tol_pt"]
    rank = {"header": 0, "body": 1, "footer": 2}
    out: list[TextBlock] = []
    for band in ("header", "body", "footer"):
        bs = sorted((b for b in blocks if b.band == band), key=lambda b: -b.bbox[3])
        rows: list[list[TextBlock]] = []
        for b in bs:
            if rows and abs(rows[-1][0].bbox[3] - b.bbox[3]) <= tol:
                rows[-1].append(b)
            else:
                rows.append([b])
        for r in rows:
            out.extend(sorted(r, key=lambda b: b.bbox[0]))
    assert len(out) == len(blocks) and rank
    return out
