"""Summaries and embedding text (plan 4.6).

X12 writes one or two sentences on which questions a chunk answers, only through ``ask``. Number guard: every
number in the summary must appear in the chunk; on failure the request is retried once, then the chunk is indexed
without a summary. A chunk whose content hash is unchanged keeps its previous summary (no call).

Embedding text = header + content + summary + glossary expansions of the abbreviations the chunk uses. With an
embedding client, vectors are stored by the hash of that text; unchanged text reuses its vector.
"""
from __future__ import annotations

import re

from .. import logging_ as lg
from ..config import Config
from ..model import prompts
from ..model.client import ModelClient, ask
from ..model.embeddings import CachedEmbeddings
from ..model.validators import check_x12
from .model import Chunk, ChunkSet


def summarise(chunks: list[Chunk], model: ModelClient | None, previous: ChunkSet | None = None) -> dict[str, int]:
    old = {c.content_hash: c for c in previous.chunks} if previous else {}
    counts = {"reused": 0, "written": 0, "rejected": 0, "skipped": 0}
    for ch in chunks:
        prev = old.get(ch.content_hash)
        if prev is not None and prev.summary:
            ch.summary, ch.summary_notes = prev.summary, list(prev.summary_notes)
            counts["reused"] += 1
            continue
        if model is None:
            counts["skipped"] += 1
            continue
        notes: list[str] = []
        for attempt in (1, 2):
            result = ask(model, prompts.x12_chunk_summary(ch.chunk_type, ch.content, attempt))
            if result is None:
                break
            summary, problems = check_x12(result, ch.text())
            if summary:
                ch.summary = summary
                break
            notes += problems
        if ch.summary:
            counts["written"] += 1
        elif notes:
            counts["rejected"] += 1
        ch.summary_notes = notes
    lg.log_event("summarise", "done", **counts)
    return counts


def embedding_text(ch: Chunk, cfg: Config) -> str:
    seen = ch.text() + (f"\n{ch.summary}" if ch.summary else "")
    terms = [f"{abbr}: {full}" for abbr, full in cfg.chunks.glossary.items() if re.search(rf"\b{re.escape(abbr)}\b", seen)]
    return "\n\n".join(p for p in (ch.text(), ch.summary or "", "\n".join(terms)) if p)


def fill_embedding_text(chunks: list[Chunk], cfg: Config) -> list[Chunk]:
    """The embedding text is not stored in chunks.jsonl (it is header, content, summary and glossary terms, a third of
    the file): rebuild it, exactly as it was embedded, wherever a vector is made or looked up."""
    for ch in chunks:
        if not ch.embedding_text:
            ch.embedding_text = embedding_text(ch, cfg)
    return chunks


def embed(chunks: list[Chunk], cfg: Config, client: CachedEmbeddings | None) -> int:
    """Set every chunk's embedding text; with a client, store its vector and record the cache key it is under."""
    for ch in chunks:
        ch.embedding_text = embedding_text(ch, cfg)
    if client is None:
        return 0
    vectors = client.embed([c.embedding_text for c in chunks])
    for ch, v in zip(chunks, vectors, strict=True):
        ch.vector_key = client.key(ch.embedding_text) if v else None
    return len(vectors)
