"""Chunks and their embeddings in Azure Cosmos DB for NoSQL (free tier), with hybrid search (plan 4.7, 5.7).

One item per chunk in container ``chunks`` (partition key ``/doc_id``): the chunk as in ``chunks.jsonl``, a
``text`` field for the full-text index (breadcrumb, content, summary, glossary expansions, codes) and the 3,072-number
``vector`` (text-embedding-3-large) under a DiskANN index. Search runs a BM25 query and a vector query in Cosmos and
fuses the two rankings with RRF here (Cosmos' own RRF cannot filter the keyword side without also filtering the vector
side); the ranking rules and expansion afterwards are the same code as the local backend (``rank_and_expand``).

    AZURE_COSMOS_ENDPOINT   https://<account>.documents.azure.com:443/
    AZURE_COSMOS_KEY        account key (never logged)
    AZURE_COSMOS_DATABASE   default "tmp"
    AZURE_COSMOS_CONTAINER  default "chunks"
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from typing import Any

from .... import logging_ as lg
from .... import tracing
from ....config import Config
from ....errors import PipelineError
from ....retrieval.search import STOPWORDS, Hit, SearchRequest, glossary_terms, rank_and_expand
from ....retrieval.store import User
from ...chunking.model import Chunk, ChunkSet
from ..embeddings import EmbeddingClient, text_key

_BAD_ID = re.compile(r"[/\\?#]")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]*")
_CHUNK_FIELDS = {f.name for f in fields(Chunk)}
_UPLOAD_ONLY = {"embedding_text"}      # needed to embed, not to answer: a fifth of each item, so not read back
MAX_TERMS = 12


class CosmosError(PipelineError):
    """Cosmos DB is not configured or refused a request."""


@dataclass(frozen=True)
class CosmosConfig:
    endpoint: str
    key: str
    database: str = "tmp"
    container: str = "chunks"

    @classmethod
    def from_env(cls) -> CosmosConfig:
        endpoint, key = os.environ.get("AZURE_COSMOS_ENDPOINT", ""), os.environ.get("AZURE_COSMOS_KEY", "")
        if not endpoint or not key:
            raise CosmosError("set AZURE_COSMOS_ENDPOINT and AZURE_COSMOS_KEY (deploy/azure/provision.sh env)")
        return cls(endpoint, key, os.environ.get("AZURE_COSMOS_DATABASE", "tmp"),
                   os.environ.get("AZURE_COSMOS_CONTAINER", "chunks"))


def open_container(config: CosmosConfig | None = None) -> Any:
    try:
        from azure.cosmos import CosmosClient
    except ImportError as e:
        raise CosmosError("azure-cosmos is not installed: uv sync --extra cosmos") from e
    cfg = config or CosmosConfig.from_env()
    return CosmosClient(cfg.endpoint, credential=cfg.key).get_database_client(cfg.database) \
        .get_container_client(cfg.container)


def item_id(chunk_id: str) -> str:
    return _BAD_ID.sub("_", chunk_id)


def search_text(c: Chunk, glossary: dict[str, str]) -> str:
    """What the full-text index sees: breadcrumb, content, summary, keywords, glossary expansions and codes."""
    crumb = c.header.split("\n", 1)[0]
    parts = [crumb, c.content, c.summary or "", " ".join(c.keywords), " ".join(glossary_terms(c.content, glossary)),
             " ".join(c.metadata.get("codes") or [])]
    return "\n".join(p for p in parts if p)


def to_item(c: Chunk, doc_id: str, vector: list[float] | None, glossary: dict[str, str]) -> dict:
    item = {k: v for k, v in asdict(c).items() if k not in _UPLOAD_ONLY}       # its key (vector_key) is enough
    item.update(id=item_id(c.chunk_id), doc_id=doc_id, kind="chunk", text=search_text(c, glossary),
                vector_key=text_key(c.embedding_text or c.text()))
    if vector is not None:
        item["vector"] = vector
    return item


def to_chunk(item: dict) -> Chunk:
    return Chunk(**{k: v for k, v in item.items() if k in _CHUNK_FIELDS})


def upload(container: Any, chunks: ChunkSet, embedder: EmbeddingClient | None, glossary: dict[str, str]) -> dict:
    """Replace one document's chunks. Vectors of unchanged chunks (same embedding text) are reused from Cosmos, so
    only new or changed chunks are embedded; chunks no longer produced are deleted."""
    doc = chunks.doc_id
    existing = {it["id"]: it for it in container.query_items(
        "SELECT c.id, c.vector_key FROM c WHERE c.doc_id = @d", parameters=[{"name": "@d", "value": doc}],
        partition_key=doc)}
    vectors: dict[str, list[float]] = {}
    todo: list[Chunk] = []
    for c in chunks.chunks:
        key, old = text_key(c.embedding_text or c.text()), existing.get(item_id(c.chunk_id))
        if old is not None and old.get("vector_key") == key:
            prev = container.read_item(item_id(c.chunk_id), partition_key=doc)
            if prev.get("vector"):
                vectors[c.chunk_id] = prev["vector"]
                continue
        todo.append(c)
    if todo and embedder is not None:
        for c, v in zip(todo, embedder.embed([c.embedding_text or c.text() for c in todo]), strict=True):
            vectors[c.chunk_id] = v
    keep = set()
    for c in chunks.chunks:
        container.upsert_item(to_item(c, doc, vectors.get(c.chunk_id), glossary))
        keep.add(item_id(c.chunk_id))
    stale = [i for i in existing if i not in keep]
    for i in stale:
        container.delete_item(i, partition_key=doc)
    out = {"uploaded": len(chunks.chunks), "embedded": len(todo) if embedder is not None else 0,
           "reused": len(chunks.chunks) - len(todo), "deleted": len(stale),
           "without_vector": sum(c.chunk_id not in vectors for c in chunks.chunks)}
    lg.log_event("cosmos", "uploaded", doc_id=doc, **out)
    return out


def _access(user: User, params: list[dict]) -> str:
    """Security trimming: a chunk with no access groups is open; otherwise one of the user's groups must match."""
    if not user.groups:
        return "ARRAY_LENGTH(c.metadata.access_groups) = 0"
    params.append({"name": "@groups", "value": list(user.groups)})
    return ("(ARRAY_LENGTH(c.metadata.access_groups) = 0 OR "
            "EXISTS(SELECT VALUE g FROM g IN c.metadata.access_groups WHERE ARRAY_CONTAINS(@groups, g)))")


def where(req: SearchRequest, user: User, params: list[dict]) -> str:
    params.append({"name": "@doc", "value": req.doc_id})
    parts = ["c.doc_id = @doc", "c.kind = 'chunk'", _access(user, params)]
    if not req.include_not_applicable:
        parts.append("c.metadata.applicability = 'active'")
    return " AND ".join(parts)


def terms(query: str) -> list[str]:
    """Words for FullTextScore: lower case, no stopwords, codes kept whole (RP 0.465 -> "0.465")."""
    out: list[str] = []
    for w in _WORD.findall(query):
        w = w.lower().strip(".-")
        if len(w) > 1 and w not in STOPWORDS and w not in out:
            out.append(w)
    return out[:MAX_TERMS]


def keyword_query(req: SearchRequest, user: User, words: list[str], top: int) -> tuple[str, list[dict]]:
    """BM25 over the full-text index. FullTextScore only ranks reliably over chunks that contain a term (without the
    FullTextContainsAny filter every chunk scores alike), so the filter is part of the query."""
    params: list[dict] = [{"name": "@top", "value": top}]
    filt = where(req, user, params)
    names = []
    for i, w in enumerate(words):
        params.append({"name": f"@t{i}", "value": w})
        names.append(f"@t{i}")
    t = ", ".join(names)
    return (f"SELECT TOP @top c.id FROM c WHERE {filt} AND FullTextContainsAny(c.text, {t}) "
            f"ORDER BY RANK FullTextScore(c.text, {t})"), params


def vector_query(req: SearchRequest, user: User, vector: list[float], top: int) -> tuple[str, list[dict]]:
    params: list[dict] = [{"name": "@top", "value": top}, {"name": "@qv", "value": vector}]
    filt = where(req, user, params)
    return f"SELECT TOP @top c.id FROM c WHERE {filt} ORDER BY VectorDistance(c.vector, @qv)", params


class CosmosChunks:
    """A document's chunks read back from Cosmos (without vectors), for citations, diagrams and expansion."""

    def __init__(self, container: Any):
        self.container = container
        self._cache: dict[str, ChunkSet] = {}

    def all(self, doc_id: str) -> ChunkSet:
        if doc_id not in self._cache:
            fields_sql = ", ".join(f"c.{f}" for f in sorted(_CHUNK_FIELDS - _UPLOAD_ONLY))
            items = list(self.container.query_items(f"SELECT {fields_sql} FROM c WHERE c.doc_id = @d AND c.kind = 'chunk'",
                                                    parameters=[{"name": "@d", "value": doc_id}], partition_key=doc_id))
            self._cache[doc_id] = ChunkSet(doc_id, chunks=[to_chunk(i) for i in items])
        return self._cache[doc_id]

    def chunks(self, doc_id: str, user: User) -> ChunkSet | None:
        cs = self.all(doc_id)
        if not cs.chunks:
            return None
        return ChunkSet(cs.doc_id, cs.versions, cs.facts_revision,
                        [c for c in cs.chunks if user.may_read(c.metadata.get("access_groups") or [])])


class CosmosSearch:
    """``ChunkSearch`` over Cosmos: one hybrid query (BM25 + vectors, RRF), then the shared ranking and expansion."""

    def __init__(self, cfg: Config, container: Any, embedder: EmbeddingClient | None, chunks: CosmosChunks | None = None):
        self.cfg, self.container, self.embedder = cfg, container, embedder
        self.store = chunks or CosmosChunks(container)

    def _ids(self, sql: str, params: list[dict], doc_id: str) -> list[str]:
        return [it["id"] for it in self.container.query_items(sql, parameters=params, partition_key=doc_id)]

    def search(self, req: SearchRequest, user: User) -> list[Hit]:
        """Keyword and vector candidates from Cosmos, fused here with reciprocal rank fusion (plan 5.7)."""
        n, k = self.cfg.retrieval.candidates, self.cfg.retrieval.rrf_k
        words = terms(req.query)
        embedder = self.embedder

        def keyword() -> list[str]:
            return self._ids(*keyword_query(req, user, words, n), req.doc_id) if words else []

        def vector() -> list[str]:
            if embedder is None:
                return []
            return self._ids(*vector_query(req, user, embedder.embed([req.query])[0], n), req.doc_id)

        with tracing.span("cosmos hybrid search", "retriever", **{"tmp.terms": len(words),
                                                                  "tmp.vector": self.embedder is not None}) as sp, \
                ThreadPoolExecutor(max_workers=2) as pool:     # keyword query runs while the question is embedded
            kw_f, vec_f = lg.submit_in_context(pool, keyword), lg.submit_in_context(pool, vector)
            kw, vec = kw_f.result(), vec_f.result()
            sp.set(**{"tmp.keyword_hits": len(kw), "tmp.vector_hits": len(vec)})
        fused: dict[str, float] = {}
        for ranking in (kw, vec):
            for rank, i in enumerate(ranking, 1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (k + rank)
        visible = self.store.chunks(req.doc_id, user)
        by_id = {item_id(c.chunk_id): c for c in (visible.chunks if visible else [])}
        return rank_and_expand([(by_id[i], s) for i, s in fused.items() if i in by_id], req,
                               visible.chunks if visible else [], self.cfg)
