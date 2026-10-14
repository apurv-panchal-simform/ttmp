"""Chunks (plan part 4): one answerable unit each, built from the canonical record and the element store.

``chunks.jsonl`` holds one chunk per line after a header line; it is the source the index is rebuilt from. Vectors
are not in it (they live in ``vectors/`` by embedding-text hash, so unchanged chunks reuse theirs).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..models import SCHEMA_VERSION, SchemaError, atomic_write_text

NOT_STORED = {"embedding_text"}      # rebuilt from the chunk when needed (chunks.summarise.fill_embedding_text)


@dataclass
class Chunk:
    chunk_id: str                   # stable across reruns: "<tmp ref>:<type>:<key>"
    chunk_type: str
    header: str                     # breadcrumb line + document context line
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    field_paths: list[str] = field(default_factory=list)   # canonical fact paths the chunk states
    record_ids: list[str] = field(default_factory=list)
    element_ids: list[str] = field(default_factory=list)
    parent_id: str | None = None
    siblings: list[str] = field(default_factory=list)
    summary: str | None = None
    summary_notes: list[str] = field(default_factory=list)
    embedding_text: str = ""
    tokens: int = 0
    content_hash: str = ""
    vector_key: str | None = None   # sha256 of embedding_text when a vector was stored

    def text(self) -> str:
        return f"{self.header}\n\n{self.content}"

    def rehash(self) -> None:
        self.content_hash = hashlib.sha256(self.text().encode("utf-8")).hexdigest()[:16]


@dataclass
class ChunkSet:
    doc_id: str
    versions: dict[str, str] = field(default_factory=dict)
    facts_revision: int = 1
    chunks: list[Chunk] = field(default_factory=list)

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)

    def of(self, chunk_type: str) -> list[Chunk]:
        return [c for c in self.chunks if c.chunk_type == chunk_type]

    def dump_jsonl(self, path: str | Path) -> None:
        head = {"schema_version": SCHEMA_VERSION, "doc_id": self.doc_id, "versions": self.versions,
                "facts_revision": self.facts_revision, "chunks": len(self.chunks)}
        lines = [json.dumps(head, ensure_ascii=False)]
        lines += [json.dumps({k: v for k, v in asdict(c).items() if k not in NOT_STORED}, ensure_ascii=False, default=str)
                  for c in self.chunks]
        atomic_write_text(path, "\n".join(lines) + "\n")

    @classmethod
    def load_jsonl(cls, path: str | Path) -> ChunkSet:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        head = json.loads(lines[0])
        if head.get("schema_version", 0) > SCHEMA_VERSION:
            raise SchemaError(f"{path}: chunks schema_version is newer than supported {SCHEMA_VERSION}")
        known = {f.name for f in fields(Chunk)}
        chunks = [Chunk(**{k: v for k, v in json.loads(line).items() if k in known}) for line in lines[1:] if line]
        return cls(head["doc_id"], head.get("versions", {}), head.get("facts_revision", 1), chunks)
