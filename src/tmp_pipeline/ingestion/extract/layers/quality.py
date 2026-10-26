"""Text-quality score (plan section 1.2): share of dictionary words in a region.

A low score marks broken font encoding, as on the sample's diagram sheets
("PĞ.Ğstrgan ManahĞmĞnt" for "Pedestrian Management"). None means there was nothing to judge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_CID = re.compile(r"\(cid:\d+\)")           # glyph the extractor could not map to a character
MIN_WORD_LEN = 3


@lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    return frozenset(resources.files("tmp_pipeline").joinpath("data", "words.txt").read_text(encoding="utf-8").split())


def _known(word: str) -> bool:
    d = _dictionary()
    if word in d:
        return True
    for suffix in ("s", "es", "ed", "ing", "'s"):       # cheap inflection fallback
        if word.endswith(suffix) and word[: -len(suffix)] in d:
            return True
    return False


@dataclass
class Quality:
    score: float | None      # share of dictionary words; None = nothing to judge
    n_words: int
    n_cid: int               # unmapped-glyph markers, a direct sign of broken encoding

    def is_low(self, threshold: float, min_words: int) -> bool:
        if self.score is None:
            return self.n_cid > 0
        return self.score < threshold and (self.n_words >= min_words or self.n_cid > 0)


def assess(text: str) -> Quality:
    n_cid = len(_CID.findall(text))
    words = [w.lower() for w in _WORD.findall(_CID.sub(" ", text)) if len(w) >= MIN_WORD_LEN]
    if not words:
        return Quality(None, 0, n_cid)
    return Quality(sum(_known(w) for w in words) / len(words), len(words), n_cid)


def text_quality(text: str) -> float | None:
    return assess(text).score
