"""Cosmos DB backend (optional `cosmos` package) against an in-memory fake container: upload with vector reuse and
stale-chunk deletion, query building (filters, access trimming, keyword and vector halves), and hybrid search that
fuses both rankings and reuses the shared ranking rules."""
import math
import re

from test_chunks import _chunks
from tmp_pipeline.config import load_config
from tmp_pipeline.cosmos.store import (
    CosmosChunks,
    CosmosSearch,
    item_id,
    keyword_query,
    search_text,
    terms,
    upload,
    vector_query,
)
from tmp_pipeline.model.embeddings import FakeEmbeddings
from tmp_pipeline.retrieval.hybrid import SearchRequest
from tmp_pipeline.retrieval.store import User

CFG = load_config()


class FakeContainer:
    """Enough of a Cosmos container: upsert, read, delete, and the four query shapes the backend sends."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.queries: list[str] = []

    def upsert_item(self, item):
        self.items[(item["doc_id"], item["id"])] = dict(item)

    def read_item(self, item, partition_key):
        return self.items[(partition_key, item)]

    def delete_item(self, item, partition_key):
        del self.items[(partition_key, item)]

    def query_items(self, query, parameters, partition_key):
        self.queries.append(query)
        p = {x["name"]: x["value"] for x in parameters}
        docs = [it for (d, _), it in self.items.items() if d == partition_key]
        if "ARRAY_CONTAINS(@types" in query:
            docs = [it for it in docs if it["chunk_type"] in p["@types"]]
        if "FullTextContainsAny" in query:
            words = [v for k, v in p.items() if re.fullmatch(r"@t\d+", k)]
            scored = [(sum(it["text"].lower().count(w) for w in words), it) for it in docs]
            docs = [it for s, it in sorted(scored, key=lambda x: -x[0]) if s]
        elif "VectorDistance" in query:
            q = p["@qv"]
            docs = sorted((it for it in docs if it.get("vector")), key=lambda it: -_cos(q, it["vector"]))
        top = p.get("@top", len(docs))
        if query.startswith("SELECT c.id, c.vector_key"):
            return [{"id": it["id"], "vector_key": it.get("vector_key")} for it in docs]
        if query.startswith("SELECT TOP"):
            return [{"id": it["id"]} for it in docs[:top]]
        return [dict(it) for it in docs]


def _cos(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _loaded():
    cs = _chunks()[0]
    c = FakeContainer()
    first = upload(c, cs, FakeEmbeddings(dims=8), dict(CFG.chunks.glossary))
    return cs, c, first


def test_upload_embeds_once_reuses_vectors_and_deletes_stale_chunks():
    cs, c, first = _loaded()
    assert first["uploaded"] == len(cs.chunks) == first["embedded"] and first["without_vector"] == 0
    again = upload(c, cs, FakeEmbeddings(dims=8), {})
    assert again["embedded"] == 0 and again["reused"] == len(cs.chunks)
    cs.chunks[0].embedding_text += " changed"
    gone = cs.chunks.pop()
    third = upload(c, cs, FakeEmbeddings(dims=8), {})
    assert third["embedded"] == 1 and third["deleted"] == 1 and (cs.doc_id, item_id(gone.chunk_id)) not in c.items


def test_items_carry_search_text_and_round_trip_to_chunks():
    cs, c, _ = _loaded()
    back = CosmosChunks(c).all(cs.doc_id)
    assert {x.chunk_id for x in back.chunks} == {x.chunk_id for x in cs.chunks}
    sl = next(x for x in cs.chunks if x.chunk_type == "speed_limit")
    text = search_text(sl, dict(CFG.chunks.glossary))
    assert "RP 0.465" in text and sl.header.split("\n")[0] in text
    assert item_id("a/b?c#d\\e") == "a_b_c_d_e"


def test_queries_filter_by_document_access_type_and_scope():
    req = SearchRequest("doc", "What happens if there's a spill on RP 0.465?", ("procedure",), scope="site_specific")
    assert terms(req.query) == ["happens", "spill", "rp", "0.465"]
    sql, params = keyword_query(req, User(("designers",)), terms(req.query), 20)
    p = {x["name"]: x["value"] for x in params}
    assert "FullTextContainsAny(c.text, @t0, @t1, @t2, @t3)" in sql and "ORDER BY RANK FullTextScore" in sql
    assert "c.doc_id = @doc" in sql and "c.metadata.applicability = 'active'" in sql and p["@scope"] == "site_specific"
    assert "ARRAY_CONTAINS(@groups, g)" in sql and p["@groups"] == ["designers"] and p["@types"] == ["procedure"]
    vsql, vparams = vector_query(SearchRequest("doc", "q"), User(), [0.1, 0.2], 5)
    assert "ORDER BY VectorDistance(c.vector, @qv)" in vsql and "ARRAY_LENGTH(c.metadata.access_groups) = 0" in vsql


def test_hybrid_search_fuses_keyword_and_vector_rankings():
    cs, c, _ = _loaded()
    s = CosmosSearch(CFG, c, FakeEmbeddings(dims=8))
    req = SearchRequest(cs.doc_id, "temporary speed limit RP 0.465", k=6)
    by_keyword = s._ids(*keyword_query(req, User(), terms(req.query), 20), cs.doc_id)
    assert by_keyword[0].endswith("speed-limit-0")                                     # the keyword side ranks it first
    hits = s.search(req, User())
    # fake vectors are content hashes, so where the fusion puts it among the top few is arbitrary: found, not ranked
    assert "speed_limit" in {h.chunk.chunk_type for h in hits if h.via == "match"}
    assert any("FullTextContainsAny" in q for q in c.queries) and any("VectorDistance" in q for q in c.queries)
    assert CosmosSearch(CFG, c, None).search(SearchRequest(cs.doc_id, "Stop Go", k=2), User())
