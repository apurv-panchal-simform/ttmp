"""Result cache for DI calls, keyed so a changed file, model, API version, feature set or page set misses."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class CacheMiss(Exception):
    pass


def cache_key(file_sha256: str, model: str, api_version: str, features: list[str], pages: str | None) -> str:
    raw = "|".join([file_sha256, model, api_version, ",".join(sorted(features)), pages or "all"])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class DiCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.path(key).exists()

    def get(self, key: str) -> dict:
        if not self.has(key):
            raise CacheMiss(key)
        return json.loads(self.path(key).read_text(encoding="utf-8"))

    def put(self, key: str, result: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(key).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
