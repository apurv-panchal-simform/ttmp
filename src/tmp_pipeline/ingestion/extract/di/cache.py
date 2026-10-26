"""Result cache for DI calls, keyed so a changed file, model, API version, feature set or page set misses."""
from __future__ import annotations

import hashlib

from ....cache import CacheMiss, FileCache

__all__ = ["CacheMiss", "DiCache", "cache_key"]


def cache_key(file_sha256: str, model: str, api_version: str, features: list[str], pages: str | None) -> str:
    raw = "|".join([file_sha256, model, api_version, ",".join(sorted(features)), pages or "all"])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class DiCache(FileCache):
    """A FileCache holding one DI analyzeResult per key."""
