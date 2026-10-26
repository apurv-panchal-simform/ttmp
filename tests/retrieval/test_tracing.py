"""Tracing to Langfuse (optional `tracing` extra): one trace per question with tool and model calls as children,
Langfuse observation types and GenAI attributes, a session per conversation, and no prompt or answer text unless
TMP_TRACE_CONTENT=1."""
import pytest

sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from test_llm import FakeOpenAI  # noqa: E402
from test_retrieval import _retriever, _session, built, store  # noqa: E402,F401  (built, store: fixtures)
from tmp_pipeline import tracing  # noqa: E402
from tmp_pipeline.llm import AzureChat, ChatConfig  # noqa: E402
from tmp_pipeline.retrieval import prompts  # noqa: E402

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    p = sdk.TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(p)
    yield


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.delenv("TMP_TRACE_CONTENT", raising=False)
    EXPORTER.clear()
    yield


def _by_name():
    return {s.name: s for s in EXPORTER.get_finished_spans()}


def test_a_question_is_one_trace_with_a_search_and_a_model_child(store):  # noqa: F811
    s = _session(store)
    _retriever(store).ask("How long is the 30 km/h zone?", s)
    spans = _by_name()
    root = spans["tmp.ask"]
    a = root.attributes
    assert a["langfuse.trace.name"] == "tmp.ask" and a["langfuse.session.id"] == s.session_id
    assert a["langfuse.observation.type"] == "chain" and a["langfuse.trace.tags"] == ("answer",)
    assert a["tmp.method"] == "safe (no model)" and a["tmp.check_problems"] == 0 and "tmp.latency_ms.retrieve" in a
    for child in ("search_chunks", "model Q3"):
        assert spans[child].parent.span_id == root.context.span_id, child
    assert spans["search_chunks"].attributes["langfuse.observation.type"] == "retriever"
    assert spans["model Q3"].attributes["tmp.outcome"] == "skipped"
    assert "langfuse.observation.input" not in a and "langfuse.observation.output" not in a   # no text by default


def test_text_is_sent_only_when_switched_on(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TMP_TRACE_CONTENT", "1")
    _retriever(store).ask("Is this TMP approved?", _session(store))
    spans = _by_name()
    root = spans["tmp.ask"].attributes
    assert root["langfuse.observation.input"] == "Is this TMP approved?"
    assert "KTS 26-1581" in root["langfuse.observation.output"]
    assert spans["search_chunks"].attributes["langfuse.observation.output"].startswith("[")


def test_live_model_calls_are_generations_with_usage_and_never_the_key(monkeypatch):
    monkeypatch.setenv("TMP_TRACE_CONTENT", "1")
    chat = AzureChat(ChatConfig("https://x.openai.azure.com", "secret-key", {"gpt-6-luna": "luna-dz"}),
                     FakeOpenAI({"answer": "ok"}))
    chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", []))
    a = _by_name()["Q3 gpt-6-luna"].attributes
    assert a["langfuse.observation.type"] == "generation" and a["gen_ai.request.model"] == "gpt-6-luna"
    assert a["gen_ai.response.model"] == "gpt-6-sol-2026" and a["gen_ai.usage.input_tokens"] == 5
    assert a["tmp.deployment"] == "luna-dz" and a["langfuse.observation.metadata.prompt_id"] == "Q3"
    assert '"role": "system"' in a["langfuse.observation.input"] and a["langfuse.observation.output"] == '{"answer": "ok"}'
    assert "secret-key" not in str(dict(a))


def test_failed_live_calls_are_marked_as_errors():
    from tmp_pipeline.llm import ModelUnavailableError
    chat = AzureChat(ChatConfig("https://x", "k"), FakeOpenAI({"answer": "x"}, status=400))
    with pytest.raises(ModelUnavailableError):
        chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", []))
    span = _by_name()["Q3 gpt-6-luna"]
    assert span.status.status_code.name == "ERROR" and span.attributes["langfuse.observation.level"] == "ERROR"


def test_configure_needs_both_langfuse_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    assert tracing.configure() is False


def test_the_exporter_targets_langfuse_otlp_with_basic_auth(monkeypatch):
    import base64

    from opentelemetry.exporter.otlp.proto.http import trace_exporter
    seen = {}

    class Recorder(trace_exporter.OTLPSpanExporter):
        def __init__(self, endpoint=None, headers=None, **kw):
            seen.update(endpoint=endpoint, headers=headers)
            super().__init__(endpoint=endpoint, headers=headers, **kw)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", Recorder)
    monkeypatch.setattr(tracing._otel, "set_tracer_provider", lambda p: None)     # keep the test provider
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com/")
    assert tracing.configure() is True
    assert seen["endpoint"] == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    assert seen["headers"]["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert seen["headers"]["x-langfuse-ingestion-version"] == "4"
    monkeypatch.setattr(tracing, "_configured", False)


def test_model_calls_carry_cost_and_each_question_sums_its_calls():
    import json as _json

    from tmp_pipeline.llm import cost
    assert cost("gpt-6-sol", 1_000_000, 100_000) == {"input": 2.0, "output": 1.0, "total": 3.0}
    assert cost("no-such-model", 10, 10) is None

    class Usage(FakeOpenAI):
        def __call__(self, method, url, headers, body):
            status, h, data = super().__call__(method, url, headers, body)
            reply = _json.loads(data)
            reply["usage"] = {"prompt_tokens": 1000, "completion_tokens": 200}
            return status, h, _json.dumps(reply).encode()

    chat = AzureChat(ChatConfig("https://x", "k"), Usage({"answer": "ok"}))
    with tracing.cost_scope() as total:
        chat.complete(prompts.q4_repair(["p"], "a", "Q?", "CTX", "now"))
        chat.complete(prompts.q4_repair(["p"], "a", "Q2?", "CTX", "now"))
    a = _by_name()["Q4 gpt-6-sol"].attributes
    assert _json.loads(a["langfuse.observation.usage_details"]) == {"input": 1000, "output": 200, "total": 1200}
    assert _json.loads(a["langfuse.observation.cost_details"]) == {"input": 0.002, "output": 0.002, "total": 0.004}
    assert total.calls == 2 and total.usd == pytest.approx(0.008) and total.input_tokens == 2000


def test_the_question_span_holds_the_total_cost(store):  # noqa: F811
    res = _retriever(store).ask("Is this TMP approved?", _session(store))
    a = _by_name()["tmp.ask"].attributes
    assert res.cost_usd == 0.0 and a["tmp.cost_usd"] == 0.0 and a["tmp.model_calls"] == 0
    assert a["langfuse.trace.metadata.cost_usd"] == "0.000000"
