"""Phase 5 stages (plan 6.1 steps 16-17): ``build_chunks`` (types, size policy, headers, metadata, lineage check)
and ``summarise_and_embed`` (X12 with the number guard, embedding text, vectors). Both read only saved results, so
a chunker change or a reviewer correction rebuilds chunks without reading the PDF again (plan 6.2)."""
from __future__ import annotations

from pathlib import Path

from .. import logging_ as lg
from ..cache import FileCache
from ..catalog import Catalog, load_catalog
from ..config import Config
from ..facts.model import Facts
from ..model.client import ModelClient
from ..model.embeddings import CachedEmbeddings, EmbeddingClient
from ..models import ElementStore
from ..records import RecordStore
from ..versions import version_stamp
from .build import Builder
from .context import build_context
from .keywords import chunk_keywords
from .lineage import check
from .meta import add_metadata
from .model import ChunkSet
from .summarise import embed, summarise


def build_chunks(facts: Facts, records: RecordStore, store: ElementStore, cfg: Config,
                 access_groups: list[str] | None = None, catalog: Catalog | None = None) -> tuple[ChunkSet, dict]:
    ctx = build_context(facts, records, store, cfg)
    chunks = Builder(facts, records, store, cfg, ctx).build()
    add_metadata(chunks, facts, ctx, cfg, access_groups)
    catalog = catalog or load_catalog()
    by_id = {f.fact_id: f for f in facts.facts}
    for c in chunks:                       # after the metadata: the keywords of a diagram chunk need its tmd_ids
        c.keywords = chunk_keywords(c, facts, catalog, by_id)
    lineage = check(chunks, facts, records, store)
    counts: dict[str, int] = {}
    for c in chunks:
        counts[c.chunk_type] = counts.get(c.chunk_type, 0) + 1
    lg.log_event("build_chunks", "done", chunks=len(chunks), by_type=counts,
                 over_max=sum(c.tokens > cfg.chunks.hard_max for c in chunks), lineage=lineage["complete"],
                 gaps=len(lineage["gaps"]))
    return ChunkSet(facts.doc_id, version_stamp(), facts.revision, chunks), lineage


def summarise_and_embed(chunks: ChunkSet, cfg: Config, model: ModelClient | None, embedder: EmbeddingClient | None,
                        out_dir: Path, previous: ChunkSet | None = None) -> dict:
    counts = summarise(chunks.chunks, model, previous)
    client = CachedEmbeddings(embedder, FileCache(out_dir / "vectors")) if embedder is not None else None
    counts["embedded"] = embed(chunks.chunks, cfg, client)
    return counts
