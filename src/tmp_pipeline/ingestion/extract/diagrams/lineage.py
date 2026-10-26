"""Image lineage (plan section 2.6): which pictures are reused across sheets, by perceptual hash."""
from __future__ import annotations

from .models import ImageInfo


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def link_reuse(by_page: dict[int, list[ImageInfo]], max_bits: int) -> None:
    """Fill ``reused_on`` with the other pages that carry the same picture (hash distance up to ``max_bits``)."""
    pages = sorted(by_page)
    for p in pages:
        for im in by_page[p]:
            if not im.phash:
                continue
            im.reused_on = sorted({q for q in pages if q != p for other in by_page[q]
                                   if other.phash and hamming(im.phash, other.phash) <= max_bits})
