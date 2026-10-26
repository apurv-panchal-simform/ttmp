"""Live services for Phase 5 (optional `search` package) against a fake HTTP transport: the AI Search index schema
(plan 4.7), the delete-upload-count push, Azure OpenAI embeddings, and replay of recorded vectors."""
import json

import pytest

from test_chunks import _chunks
from tmp_pipeline.cache import FileCache
from tmp_pipeline.ingestion.embedding.embeddings import (
    CachedEmbeddings,
    FakeEmbeddings,
    RecordingEmbeddings,
    ReplayEmbeddings,
)
from tmp_pipeline.ingestion.embedding.search.azure_openai import AzureEmbeddings, OpenAIConfig
from tmp_pipeline.ingestion.embedding.search.index import (
    IndexCountError,
    SearchConfig,
    SearchIndex,
    document,
    index_schema,
)
from tmp_pipeline.llm import ModelUnavailableError
from tmp_pipeline.transport import ServiceError, TransientServiceError, with_retry


class FakeSearch:
    """Just enough of the AI Search REST API: index docs, search by doc_id filter, count."""

    def __init__(self, lag=0):
        self.docs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.lag = lag

    def __call__(self, method, url, headers, body):
        assert headers["api-key"] == "secret" and "api-version=" in url
        path = url.split("?")[0].split(".net", 1)[1]
        self.calls.append((method, path))
        data = json.loads(body) if body else {}
        if method == "PUT":
            return 201, {}, json.dumps(data).encode()
        if path.endswith("/docs/index"):
            for a in data["value"]:
                if a["@search.action"] == "delete":
                    self.docs.pop(a["id"], None)
                else:
                    self.docs[a["id"]] = a
            return 200, {}, json.dumps({"value": [{"key": a["id"], "status": True} for a in data["value"]]}).encode()
        doc_id = data["filter"].split("'")[1]
        hits = [d for d in self.docs.values() if d["doc_id"] == doc_id]
        if data.get("count"):
            n = max(0, len(hits) - self.lag)
            self.lag = max(0, self.lag - 1)
            return 200, {}, json.dumps({"@odata.count": n, "value": []}).encode()
        page = hits[data.get("skip", 0):data.get("skip", 0) + data["top"]]
        return 200, {}, json.dumps({"value": [{"id": d["id"]} for d in page]}).encode()


def _index(fake, **kw):
    return SearchIndex(SearchConfig("https://x.search.windows.net", "secret"), fake, sleep=lambda s: None, **kw)


def test_the_index_schema_has_the_plan_settings():
    s = index_schema()
    fields = {f["name"]: f for f in s["fields"]}
    assert s["name"] == "tmp-chunks-v1" and fields["id"]["key"]
    assert fields["content"]["searchable"] and fields["summary"]["searchable"]
    assert fields["codes"]["analyzer"] == "keyword" and fields["access_groups"]["filterable"]
    v = fields["content_vector"]
    assert v["dimensions"] == 3072 and v["stored"] is False and v["vectorSearchProfile"] == "hnsw-sq"
    comp = s["vectorSearch"]["compressions"][0]
    assert comp["kind"] == "scalarQuantization" and comp["rerankWithOriginalVectors"] is True
    assert s["vectorSearch"]["algorithms"][0]["kind"] == "hnsw"
    assert [f["fieldName"] for f in s["semantic"]["configurations"][0]["prioritizedFields"]
            ["prioritizedContentFields"]] == ["content", "summary"]
    assert {"tmd_ids", "rp_from", "conflict_ids", "status", "scope", "is_boilerplate"} <= set(fields)


def test_push_deletes_by_doc_id_uploads_and_checks_the_count():
    cs, *_ = _chunks()
    fake = FakeSearch()
    idx = _index(fake, batch=10)
    fake.docs["stale"] = {"id": "stale", "doc_id": cs.doc_id}
    fake.docs["other"] = {"id": "other", "doc_id": "sha256:other"}
    res = idx.push(cs, {}, {"TMD": "traffic management diagram"})
    assert res == {"deleted": 1, "uploaded": len(cs.chunks), "counted": len(cs.chunks)}
    assert "stale" not in fake.docs and "other" in fake.docs
    paths = [p for _, p in fake.calls]
    assert paths.index("/indexes/tmp-chunks-v1/docs/index") > paths.index("/indexes/tmp-chunks-v1/docs/search")
    doc = next(d for d in fake.docs.values() if d.get("chunk_type") == "doc_card")
    assert ":" not in doc["id"] and doc["chunk_id"].endswith(":doc_card:card") and "content_vector" not in doc
    assert doc["tmp_ref_aliases"] == ["KTS 26-1580"] and doc["has_conflict"] is True


def test_a_lagging_count_is_waited_for_and_a_wrong_count_raises():
    cs, *_ = _chunks()
    assert _index(FakeSearch(lag=2)).push(cs, {}, {})["counted"] == len(cs.chunks)
    with pytest.raises(IndexCountError):
        _index(FakeSearch(lag=50)).push(cs, {}, {}, checks=2)


def test_documents_carry_their_vector_when_one_is_stored():
    cs, *_ = _chunks()
    c = cs.chunks[0]
    c.vector_key = "k"
    assert document(c, [0.1, 0.2], {})["content_vector"] == [0.1, 0.2]


def test_azure_embeddings_batch_and_keep_input_order():
    seen = []

    def transport(method, url, headers, body):
        req = json.loads(body)
        seen.append((url, req["dimensions"], len(req["input"])))
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(req["input"])][::-1]
        return 200, {}, json.dumps({"data": data}).encode()

    emb = AzureEmbeddings(OpenAIConfig("https://x.openai.azure.com", "k"), transport, batch=2)
    assert emb.embed(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert [n for *_, n in seen] == [2, 1] and "/deployments/text-embedding-3-large/embeddings" in seen[0][0]


def test_embedding_errors_are_typed_and_throttling_is_retried():
    calls = []

    def throttled(method, url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            return 429, {"Retry-After": "0"}, b""
        return 200, {}, json.dumps({"data": [{"index": 0, "embedding": [1.0]}]}).encode()

    emb = AzureEmbeddings(OpenAIConfig("https://x", "k"), with_retry(throttled, sleep=lambda s: None))
    assert emb.embed(["a"]) == [[1.0]] and len(calls) == 2
    bad = AzureEmbeddings(OpenAIConfig("https://x", "k"), lambda *a: (400, {}, b"bad request"))
    with pytest.raises(ServiceError, match="HTTP 400"):
        bad.embed(["a"])
    down = AzureEmbeddings(OpenAIConfig("https://x", "k"), lambda *a: (503, {}, b""))
    with pytest.raises(TransientServiceError):
        down.embed(["a"])


def test_missing_credentials_are_reported_without_a_call(monkeypatch):
    for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ServiceError, match="AZURE_OPENAI_ENDPOINT"):
        OpenAIConfig.from_env()
    with pytest.raises(ServiceError, match="AZURE_SEARCH_ENDPOINT"):
        SearchConfig.from_env()


def test_recorded_vectors_replay_offline_and_a_missing_one_is_an_error(tmp_path):
    recorder = RecordingEmbeddings(FakeEmbeddings(dims=3072, model="text-embedding-3-large"), tmp_path)
    vectors = recorder.embed(["alpha", "beta"])
    replay = ReplayEmbeddings(tmp_path)
    assert replay.embed(["beta", "alpha"]) == [vectors[1], vectors[0]]
    with pytest.raises(ModelUnavailableError):
        replay.embed(["gamma"])


def test_cached_embeddings_ask_only_for_new_text(tmp_path):
    fake = FakeEmbeddings()
    cached = CachedEmbeddings(fake, FileCache(tmp_path))
    cached.embed(["a", "b"])
    cached.embed(["a", "b", "c"])
    assert fake.calls == [["a", "b"], ["c"]]


# --- Phase 6: search_chunks against AI Search (plan 5.7) ----------------------------------------------------------

class FakeQuery:
    """AI Search docs/search for queries: a filtered fetch-all, or a keyword query answered by naive matching."""

    def __init__(self, docs):
        self.docs, self.bodies = docs, []

    def __call__(self, method, url, headers, body):
        data = json.loads(body)
        self.bodies.append(data)
        if data["search"] == "*":
            return 200, {}, json.dumps({"value": self.docs[data.get("skip", 0):][:data["top"]]}).encode()
        words = data["search"].lower().split()
        hits = [dict(d, **{"@search.score": sum(w in d["content"].lower() for w in words)}) for d in self.docs]
        hits = sorted((h for h in hits if h["@search.score"]), key=lambda h: -h["@search.score"])
        return 200, {}, json.dumps({"value": hits[:data["top"]]}).encode()


def test_azure_search_filters_trim_by_access_and_reuse_the_ranking_rules():
    from tmp_pipeline.config import load_config
    from tmp_pipeline.ingestion.embedding.search.query import AzureSearch, filter_for, to_chunk
    from tmp_pipeline.retrieval.search import SearchRequest
    from tmp_pipeline.retrieval.store import User
    cs = _chunks()[0]
    docs = [document(c, None, {}) for c in cs.chunks]
    back = to_chunk(docs[0])
    assert (back.chunk_id, back.chunk_type, back.content) == (cs.chunks[0].chunk_id, cs.chunks[0].chunk_type,
                                                             cs.chunks[0].content)
    assert back.metadata["tmp_ref"] == cs.chunks[0].metadata["tmp_ref"]
    req = SearchRequest(cs.doc_id, "speed limit")
    f = filter_for(req, User(("designers", "o'neil")))
    assert f.startswith(f"doc_id eq '{cs.doc_id}' and applicability eq 'active'")
    assert "access_groups/any(g: search.in(g, 'designers,o''neil', ','))" in f
    assert filter_for(SearchRequest("d", "q"), User()).endswith("not access_groups/any()")
    fake = FakeQuery(docs)
    s = AzureSearch(load_config(), SearchConfig("https://s.search.windows.net", "secret"),
                    FakeEmbeddings(dims=4), fake)
    hits = s.search(SearchRequest(cs.doc_id, "temporary speed limit", k=2), User())
    assert hits and hits[0].chunk.chunk_type == "speed_limit"
    query = next(b for b in fake.bodies if b["search"] != "*")
    assert query["vectorQueries"][0]["fields"] == "content_vector" and len(query["vectorQueries"][0]["vector"]) == 4
    assert query["searchFields"] == "header,content,summary,keywords,glossary_terms,codes"
