"""Embedding clients (plan 4.6, 4.7). The core holds only the interface, a deterministic fake, a cache that reuses
vectors of unchanged text, and record/replay fixtures; the live Azure OpenAI client lives in the optional ``search``
package. Vectors are keyed by model, dimensions and the sha256 of the text, so an unchanged chunk never pays twice."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Protocol

from ... import logging_ as lg
from ...cache import CacheMiss, FileCache, stable_key
from ...llm import ModelUnavailableError


class EmbeddingClient(Protocol):
    model: str
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeEmbeddings:
    """Deterministic unit vectors from the text's hash (tests and dry runs; no meaning)."""

    def __init__(self, dims: int = 16, model: str = "fake-embedding"):
        self.dims, self.model = dims, model
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            raw = [(seed[i % len(seed)] - 127.5) / 127.5 for i in range(self.dims)]
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            out.append([v / norm for v in raw])
        return out


class CachedEmbeddings:
    """Asks ``inner`` only for texts whose vector is not cached yet."""

    def __init__(self, inner: EmbeddingClient, cache: FileCache):
        self.inner, self.cache = inner, cache
        self.model, self.dims = inner.model, inner.dims

    def key(self, text: str) -> str:
        return stable_key(self.model, self.dims, text_key(text))

    def embed(self, texts: list[str]) -> list[list[float]]:
        found: dict[int, list[float]] = {}
        missing: list[int] = []
        for i, t in enumerate(texts):
            try:
                found[i] = self.cache.get(self.key(t))["vector"]
            except CacheMiss:
                missing.append(i)
        if missing:
            vectors = self.inner.embed([texts[i] for i in missing])
            for i, v in zip(missing, vectors, strict=True):
                self.cache.put(self.key(texts[i]), {"model": self.model, "dims": self.dims, "vector": v})
                found[i] = v
        lg.log_event("embed", "batch", texts=len(texts), reused=len(texts) - len(missing), embedded=len(missing))
        return [found[i] for i in range(len(texts))]


class RecordingEmbeddings(CachedEmbeddings):
    """Records every vector into a fixtures folder (the same layout ``ReplayEmbeddings`` reads)."""

    def __init__(self, inner: EmbeddingClient, fixtures: str | Path):
        super().__init__(inner, FileCache(fixtures))


class ReplayEmbeddings:
    """Answers from recorded fixtures only; a text with no recording raises ``ModelUnavailableError``."""

    def __init__(self, fixtures: str | Path, model: str = "text-embedding-3-large", dims: int = 3072):
        self.cache, self.model, self.dims = FileCache(fixtures), model, dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            try:
                out.append(self.cache.get(stable_key(self.model, self.dims, text_key(t)))["vector"])
            except CacheMiss:
                raise ModelUnavailableError(f"no recorded embedding for text {text_key(t)[:12]}") from None
        return out
