"""File cache shared by the DI client and the model client.

Entries are one JSON file per key. Writes are atomic, and an unreadable entry counts as a miss (and is logged),
so a crash mid-write or a hand-edited file can never poison later runs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import logging_ as lg
from .ingestion.models import atomic_write_text


class CacheMiss(Exception):
    pass


def stable_key(*parts: Any, length: int = 32) -> str:
    """Hash of the parts in order. Dicts are key-sorted, so the same content always gives the same key."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


class FileCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Any:
        p = self.path(key)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CacheMiss(key) from None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            lg.log_warning("cache", "corrupt_entry", key=key, error=str(e))
            raise CacheMiss(key) from e

    def has(self, key: str) -> bool:
        try:
            self.get(key)
        except CacheMiss:
            return False
        return True

    def put(self, key: str, value: Any) -> None:
        atomic_write_text(self.path(key), json.dumps(value, ensure_ascii=False))
