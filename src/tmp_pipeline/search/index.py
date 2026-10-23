"""Azure AI Search index ``tmp-chunks-v1`` (plan 4.7).

``content`` is searchable (BM25) and returned; ``summary`` and ``glossary_terms`` are searchable; ``codes`` use the
keyword analyzer for exact matches; every metadata field is filterable and ``access_groups`` carries security
trimming. ``content_vector`` (3,072 dimensions) uses HNSW with scalar quantisation, is not stored, and keeps the
full vectors for rescoring. A semantic configuration covers content and summary.

Re-indexing a document deletes its chunks by ``doc_id``, uploads the new ones, then checks the count.

    AZURE_SEARCH_ENDPOINT   https://<service>.search.windows.net
    AZURE_SEARCH_KEY        admin key (never logged)
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import logging_ as lg
from ..chunks.meta import DOC_FIELDS
from ..chunks.model import Chunk, ChunkSet
from ..chunks.text import glossary_matches
from ..transport import ServiceError, Transport, raise_for_status, urllib_transport, with_retry

API_VERSION = "2024-07-01"
INDEX = "tmp-chunks-v1"
DIMS = 3072

# metadata field -> (EDM type, filterable). Collections are Collection(<type>).
FILTERABLE: dict[str, str] = {
    "chunk_id": "Edm.String", "doc_id": "Edm.String", "version": "Edm.Int32", "chunk_type": "Edm.String",
    "tmp_ref": "Edm.String", "tmp_ref_aliases": "Collection(Edm.String)", "site_address": "Edm.String",
    "suburb": "Edm.String", "road_names": "Collection(Edm.String)", "client": "Edm.String",
    "contractor": "Edm.String", "rca": "Edm.String", "designer": "Edm.String", "work_start": "Edm.String",
    "work_end": "Edm.String", "zone": "Edm.String", "section_path": "Collection(Edm.String)",
    "parent_id": "Edm.String", "sibling": "Collection(Edm.String)", "element_ids": "Collection(Edm.String)",
    "page": "Edm.Int32", "pages": "Collection(Edm.Int32)", "printed_page": "Edm.Int32",
    "tmd_ids": "Collection(Edm.String)", "linked_sheets": "Collection(Edm.String)", "rp_from": "Edm.Double",
    "rp_to": "Edm.Double", "rp_values": "Collection(Edm.Double)", "scope": "Edm.String",
    "applicability": "Edm.String", "status": "Edm.String", "has_conflict": "Edm.Boolean",
    "conflict_ids": "Collection(Edm.String)", "approval_status": "Edm.String",
    "field_paths": "Collection(Edm.String)", "is_boilerplate": "Edm.Boolean", "boilerplate_hash": "Edm.String",
    "access_groups": "Collection(Edm.String)", "content_hash": "Edm.String",
}


def index_schema(name: str = INDEX, dims: int = DIMS) -> dict:
    fields: list[dict[str, Any]] = [
        {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "header", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "summary", "type": "Edm.String", "searchable": True, "retrievable": True},
        {"name": "glossary_terms", "type": "Collection(Edm.String)", "searchable": True},
        {"name": "keywords", "type": "Collection(Edm.String)", "searchable": True, "retrievable": True},
        {"name": "codes", "type": "Collection(Edm.String)", "searchable": True, "filterable": True,
         "analyzer": "keyword"},
        *({"name": k, "type": t, "filterable": True, "retrievable": True, "facetable": k in ("chunk_type", "zone")}
          for k, t in FILTERABLE.items()),
        {"name": "content_vector", "type": "Collection(Edm.Single)", "searchable": True, "retrievable": False,
         "stored": False, "dimensions": dims, "vectorSearchProfile": "hnsw-sq"},
    ]
    return {
        "name": name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [{"name": "hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine", "m": 4,
                                                                               "efConstruction": 400}}],
            "compressions": [{"name": "sq", "kind": "scalarQuantization", "rerankWithOriginalVectors": True,
                              "defaultOversampling": 4.0,
                              "scalarQuantizationParameters": {"quantizedDataType": "int8"}}],
            "profiles": [{"name": "hnsw-sq", "algorithm": "hnsw", "compression": "sq"}],
        },
        "semantic": {"configurations": [{"name": "default", "prioritizedFields": {
            "prioritizedContentFields": [{"fieldName": "content"}, {"fieldName": "summary"}]}}]},
    }


def document(c: Chunk, vector: list[float] | None, glossary: dict[str, str], card: dict | None = None) -> dict:
    """``card`` is the doc card's metadata: the document fields (plan 4.4) are stored on the card only and copied to
    every index document here, so the index can still filter any chunk by site, road, party or dates."""
    md = {**{k: v for k, v in (card or {}).items() if k in DOC_FIELDS}, **c.metadata, "chunk_type": c.chunk_type}
    doc: dict[str, Any] = {"id": c.chunk_id.replace(":", "__"), "header": c.header, "content": c.content,
                           "summary": c.summary, "content_hash": c.content_hash,
                           "glossary_terms": [f"{a} {b}" for a, b in glossary_matches(
                               f"{c.header.split(chr(10), 1)[0]}\n{c.content}\n{c.summary or ''}", glossary)],
                           "keywords": list(c.keywords),
                           "codes": md.get("codes", [])}
    for k in FILTERABLE:
        if k == "chunk_id":
            doc[k] = c.chunk_id
        elif k in ("parent_id", "sibling", "field_paths", "element_ids"):
            doc[k] = {"parent_id": c.parent_id, "sibling": c.siblings, "field_paths": c.field_paths,
                      "element_ids": c.element_ids}[k]
        elif k != "content_hash":
            doc[k] = md.get(k)
    if vector is not None:
        doc["content_vector"] = vector
    return doc


class IndexCountError(ServiceError):
    """After a push, the index does not hold exactly the chunks that were uploaded."""


@dataclass(frozen=True)
class SearchConfig:
    endpoint: str
    key: str
    index: str = INDEX

    @classmethod
    def from_env(cls, index: str = INDEX) -> SearchConfig:
        endpoint, key = os.environ.get("AZURE_SEARCH_ENDPOINT", ""), os.environ.get("AZURE_SEARCH_KEY", "")
        if not endpoint or not key:
            raise ServiceError("set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY to push to Azure AI Search")
        return cls(endpoint.rstrip("/"), key, index)


class SearchIndex:
    def __init__(self, config: SearchConfig | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, batch: int = 100):
        self.config = config or SearchConfig.from_env()
        self.transport = transport or with_retry(urllib_transport)
        self.sleep, self.batch = sleep, batch

    def _call(self, method: str, path: str, body: dict | None, what: str) -> dict:
        url = f"{self.config.endpoint}{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"
        headers = {"api-key": self.config.key, "Content-Type": "application/json"}
        status, _, data = self.transport(method, url, headers, json.dumps(body).encode("utf-8") if body else None)
        raise_for_status(status, what, data)
        return json.loads(data) if data else {}

    def create(self, dims: int = DIMS) -> dict:
        return self._call("PUT", f"/indexes/{self.config.index}", index_schema(self.config.index, dims), "create index")

    def ids_for(self, doc_id: str) -> list[str]:
        out: list[str] = []
        while True:
            res = self._call("POST", f"/indexes/{self.config.index}/docs/search",
                             {"search": "*", "filter": f"doc_id eq '{doc_id}'", "select": "id", "top": 1000,
                              "skip": len(out)}, "find chunks")
            page = [d["id"] for d in res.get("value", [])]
            out += page
            if len(page) < 1000:
                return out

    def count(self, doc_id: str) -> int:
        res = self._call("POST", f"/indexes/{self.config.index}/docs/search",
                         {"search": "*", "filter": f"doc_id eq '{doc_id}'", "count": True, "top": 0}, "count chunks")
        return int(res.get("@odata.count", 0))

    def _batch(self, actions: list[dict], what: str) -> None:
        for i in range(0, len(actions), self.batch):
            res = self._call("POST", f"/indexes/{self.config.index}/docs/index", {"value": actions[i:i + self.batch]},
                             what)
            failed = [r for r in res.get("value", []) if not r.get("status", True)]
            if failed:
                raise ServiceError(f"{what}: {len(failed)} document(s) rejected, first: {failed[0]}")

    def push(self, chunks: ChunkSet, vectors: dict[str, list[float]], glossary: dict[str, str],
             checks: int = 5) -> dict:
        """Replace one document's chunks: delete by doc_id, upload, then wait for the count to match."""
        old = self.ids_for(chunks.doc_id)
        if old:
            self._batch([{"@search.action": "delete", "id": i} for i in old], "delete chunks")
        card = next((c.metadata for c in chunks.of("doc_card")), None)
        docs = [document(c, vectors.get(c.vector_key or ""), glossary, card) for c in chunks.chunks]
        self._batch([{"@search.action": "upload", **d} for d in docs], "upload chunks")
        want, got = len(docs), -1
        for attempt in range(checks):
            got = self.count(chunks.doc_id)
            if got == want:
                break
            self.sleep(min(10.0, 1.0 * 2 ** attempt))         # the index is near real time, not immediate
        lg.log_event("index", "pushed", doc_id=chunks.doc_id, deleted=len(old), uploaded=want, counted=got)
        if got != want:
            raise IndexCountError(f"index holds {got} chunks for {chunks.doc_id}, uploaded {want}")
        return {"deleted": len(old), "uploaded": want, "counted": got}
