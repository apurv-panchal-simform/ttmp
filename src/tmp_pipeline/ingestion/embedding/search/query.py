"""``search_chunks`` against Azure AI Search (plan 5.7): the service runs the keyword and vector candidates and fuses
them (hybrid query, reciprocal rank fusion); the ranking rules and expansion are the same code as ``LocalSearch``
(``rank_and_expand``). Security trimming is a filter on ``access_groups``, so a prompt can never widen access.
"""
from __future__ import annotations

import json

from .... import tracing
from ....config import Config
from ....retrieval.search import Hit, SearchRequest, rank_and_expand
from ....retrieval.store import User
from ....transport import KeepAliveTransport, Transport, raise_for_status, with_retry
from ...chunking.model import Chunk
from ..embeddings import EmbeddingClient
from .index import API_VERSION, FILTERABLE, SearchConfig

_STRUCTURE = ("chunk_id", "parent_id", "sibling", "field_paths", "element_ids", "content_hash")
SEARCH_FIELDS = "header,content,summary,keywords,glossary_terms,codes"


def _q(text: str) -> str:
    return text.replace("'", "''")


def filter_for(req: SearchRequest, user: User) -> str:
    parts = [f"doc_id eq '{_q(req.doc_id)}'"]
    if not req.include_not_applicable:
        parts.append("applicability eq 'active'")
    groups = ",".join(_q(g) for g in user.groups)
    trim = "not access_groups/any()"
    if groups:
        trim = f"({trim} or access_groups/any(g: search.in(g, '{groups}', ',')))"
    parts.append(trim)
    return " and ".join(parts)


def to_chunk(doc: dict) -> Chunk:
    md = {k: doc.get(k) for k in FILTERABLE if k not in _STRUCTURE}
    md["codes"] = doc.get("codes") or []
    return Chunk(doc["chunk_id"], str(doc.get("chunk_type")), doc.get("header") or "", doc.get("content") or "", md,
                 list(doc.get("field_paths") or []), [], list(doc.get("element_ids") or []), doc.get("parent_id"),
                 list(doc.get("sibling") or []), doc.get("summary"), content_hash=doc.get("content_hash") or "")


class AzureSearch:
    def __init__(self, cfg: Config, config: SearchConfig | None = None, embedder: EmbeddingClient | None = None,
                 transport: Transport | None = None):
        self.cfg, self.config = cfg, config or SearchConfig.from_env()
        self.embedder = embedder
        self.transport = transport or with_retry(KeepAliveTransport())
        self._all: dict[tuple[str, tuple[str, ...]], list[Chunk]] = {}

    def _post(self, body: dict, what: str) -> dict:
        url = f"{self.config.endpoint}/indexes/{self.config.index}/docs/search?api-version={API_VERSION}"
        with tracing.span(f"ai_search {what}", "retriever", **{"tmp.index": self.config.index, "tmp.top": body.get("top"),
                                                   "tmp.hybrid": "vectorQueries" in body}) as sp:
            status, _, data = self.transport("POST", url, {"api-key": self.config.key,
                                                           "Content-Type": "application/json"},
                                             json.dumps(body).encode("utf-8"))
            raise_for_status(status, what, data)
            out = json.loads(data) if data else {}
            sp.set(**{"tmp.results": len(out.get("value", []))})
        return out

    def all_chunks(self, doc_id: str, user: User) -> list[Chunk]:
        """Every chunk of one document the user may read (for expansion), fetched once per document and user."""
        key = (doc_id, user.groups)
        if key not in self._all:
            req = SearchRequest(doc_id, "", include_not_applicable=True)
            out: list[Chunk] = []
            while True:
                res = self._post({"search": "*", "filter": filter_for(req, user), "top": 1000, "skip": len(out)},
                                 "fetch chunks")
                page = [to_chunk(d) for d in res.get("value", [])]
                out += page
                if len(page) < 1000:
                    break
            self._all[key] = out
        return self._all[key]

    def search(self, req: SearchRequest, user: User) -> list[Hit]:
        n = self.cfg.retrieval.candidates
        body: dict = {"search": req.query, "searchFields": SEARCH_FIELDS, "filter": filter_for(req, user), "top": n}
        if self.embedder is not None:
            body["vectorQueries"] = [{"kind": "vector", "vector": self.embedder.embed([req.query])[0],
                                      "fields": "content_vector", "k": n}]
        res = self._post(body, "search chunks")
        fused = [(to_chunk(d), float(d.get("@search.score") or 0.0)) for d in res.get("value", [])]
        return rank_and_expand(fused, req, self.all_chunks(req.doc_id, user), self.cfg)

