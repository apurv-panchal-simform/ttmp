"""Size policy (plan 4.2): target 200-450 tokens, hard maximum 600, small siblings merged by the builders.

A unit over the hard maximum is split between pieces (level-2 sections, bullets, then sentences), never inside one,
and each later part repeats the last sentences of the part before it. Parts are labelled "(part n of m)" and linked
as siblings.
"""
from __future__ import annotations

import re

from . import tokens

BULLET = re.compile(r"(?=(?:^|\s)(?:[•▪►▶]|- )\s*)")


def pieces(text: str) -> list[str]:
    """The smallest units a split may separate: blank-line blocks, then bullets, then sentences."""
    out: list[str] = []
    for block in [b for b in re.split(r"\n\s*\n", text) if b.strip()]:
        for item in [b for b in BULLET.split(block) if b.strip()]:
            out += tokens.sentences(item) or [item.strip()]
    return out


def pack_indices(units: list[str], budget: int, overlap: int, joiner: str = "\n") -> list[list[int]]:
    """Greedy packing as in ``pack``, returning for each part the indices of the units it holds (overlap included), so
    a caller can say where each part came from."""
    parts: list[list[int]] = []
    cur: list[int] = []

    def fits(idx: list[int], extra: int) -> bool:
        return tokens.count(joiner.join([units[i] for i in [*idx, extra]])) <= budget

    for n in range(len(units)):
        if cur and not fits(cur, n):
            parts.append(cur)
            cur = cur[-overlap:] if overlap else []
            while cur and not fits(cur, n):
                cur = cur[1:]
        cur.append(n)
    if cur and (not parts or cur != parts[-1][-len(cur):]):
        parts.append(cur)
    return parts


def pack(units: list[str], budget: int, overlap: int, joiner: str = "\n") -> list[str]:
    """Greedy packing of ``units`` into parts of at most ``budget`` tokens; each part after the first starts with
    the last ``overlap`` units of the one before. A single unit larger than the budget stays whole."""
    return [joiner.join(units[i] for i in part) for part in pack_indices(units, budget, overlap, joiner)]


def split(content: str, budget: int, overlap: int) -> list[str]:
    """``content`` as one or more parts of at most ``budget`` tokens (whole pieces only)."""
    if tokens.count(content) <= budget:
        return [content]
    return pack(pieces(content), budget, overlap)
