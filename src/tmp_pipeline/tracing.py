"""Tracing (plan 5.14 logging, 6.1 operations): OpenTelemetry spans exported to Langfuse.

Optional on two levels. Without the ``tracing`` extra installed, every helper here is a no-op. With it installed,
nothing is exported until ``configure()`` finds ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY``; spans then go
to ``LANGFUSE_HOST`` (default the EU cloud) over OTLP/HTTP at ``/api/public/otel``.

Spans carry Langfuse's own attributes (``langfuse.*``): a question is a trace (``tmp.ask``, grouped into a session per
conversation), model calls are generations (model, input, output, token usage in ``gen_ai.*``), tool calls are tools
and chunk search a retriever. Prompt, context and answer text is personal-data-adjacent (names, phone numbers): it is
sent only when ``TMP_TRACE_CONTENT=1``. Keys are never sent as attributes.
"""
from __future__ import annotations

import atexit
import base64
import contextvars
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import logging_ as lg

SERVICE = "tmp-pipeline"
DEFAULT_HOST = "https://cloud.langfuse.com"
_configured = False


@dataclass
class Cost:
    """Model spend inside one ``cost_scope`` (a question, a run): USD and tokens, summed over its model calls."""
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    unpriced: int = 0                           # calls whose model has no price


_costs: contextvars.ContextVar[tuple[Cost, ...]] = contextvars.ContextVar("tmp_costs", default=())


@contextmanager
def cost_scope() -> Iterator[Cost]:
    """Collect the cost of every model call made inside (worker threads included, via submit_in_context)."""
    total = Cost()
    token = _costs.set((*_costs.get(), total))
    try:
        yield total
    finally:
        _costs.reset(token)

try:                                            # the API alone is a cheap no-op until a provider is configured
    from opentelemetry import trace as _otel
except ImportError:                             # pragma: no cover - exercised when the extra is absent
    _otel = None  # type: ignore[assignment]


def content_enabled() -> bool:
    return os.environ.get("TMP_TRACE_CONTENT") == "1"


def configure(service: str = SERVICE) -> bool:
    """Export spans to Langfuse when its keys are set. Safe to call more than once."""
    global _configured
    if _configured:
        return True
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not (public and secret) or _otel is None:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        lg.log_warning("tracing", "not_installed", hint="uv sync --extra tracing")
        return False
    host = os.environ.get("LANGFUSE_HOST", DEFAULT_HOST).rstrip("/")
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    exporter = OTLPSpanExporter(endpoint=f"{host}/api/public/otel/v1/traces",
                                headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"})
    attrs = {"service.name": service}
    if os.environ.get("LANGFUSE_TRACING_ENVIRONMENT"):
        attrs["deployment.environment"] = os.environ["LANGFUSE_TRACING_ENVIRONMENT"]
    provider = TracerProvider(resource=Resource.create(attrs))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _otel.set_tracer_provider(provider)
    atexit.register(provider.shutdown)          # flush the last batch when a CLI exits
    _configured = True
    lg.log_event("tracing", "configured", host=host, service=service, content=content_enabled())
    return True


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """OpenTelemetry takes str, bool, int, float or lists of one of them; None is dropped."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, str | bool | int | float):
            out[k] = v
        elif isinstance(v, list | tuple) and all(isinstance(x, str) for x in v):
            out[k] = list(v)
        else:
            out[k] = str(v)
    return out


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


class _NoSpan:
    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_attributes(self, attrs: dict[str, Any]) -> None: ...
    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None: ...
    def record_exception(self, exc: BaseException) -> None: ...


class Span:
    """A thin wrapper so callers never touch OpenTelemetry types (and work without them)."""

    def __init__(self, inner: Any):
        self.inner = inner

    def set(self, **attrs: Any) -> None:
        self.inner.set_attributes(_clean(attrs))

    def input(self, value: Any) -> None:
        """What went in (prompt messages, question, tool arguments): only when content tracing is on."""
        if content_enabled():
            self.inner.set_attribute("langfuse.observation.input", _text(value))

    def output(self, value: Any) -> None:
        """What came out (completion, answer): only when content tracing is on."""
        if content_enabled():
            self.inner.set_attribute("langfuse.observation.output", _text(value))

    def usage(self, input_tokens: int | None, output_tokens: int | None, cost: dict[str, float] | None) -> None:
        """Token usage and cost of a model call: Langfuse shows them on the generation and sums them per trace;
        every open ``cost_scope`` adds them up too."""
        usage = {k: v for k, v in (("input", input_tokens), ("output", output_tokens)) if v is not None}
        if usage:
            usage["total"] = sum(usage.values())
            self.inner.set_attribute("langfuse.observation.usage_details", json.dumps(usage))
        if cost is not None:
            self.inner.set_attribute("langfuse.observation.cost_details", json.dumps(cost))
            self.inner.set_attribute("tmp.cost_usd", cost["total"])
        for c in _costs.get():
            c.calls += 1
            c.input_tokens += input_tokens or 0
            c.output_tokens += output_tokens or 0
            if cost is None:
                c.unpriced += 1
            else:
                c.usd += cost["total"]

    def error(self, exc: BaseException) -> None:
        self.inner.record_exception(exc)
        self.inner.set_attribute("langfuse.observation.level", "ERROR")
        self.inner.set_attribute("langfuse.observation.status_message", f"{type(exc).__name__}: {exc}"[:300])
        if _otel is not None and hasattr(self.inner, "set_status"):
            self.inner.set_status(_otel.Status(_otel.StatusCode.ERROR, str(exc)[:200]))


@contextmanager
def span(name: str, kind: str = "span", **attrs: Any) -> Iterator[Span]:
    """``kind`` is the Langfuse observation type: span, generation, embedding, tool, retriever, chain ..."""
    if _otel is None:
        yield Span(_NoSpan())
        return
    tracer = _otel.get_tracer("tmp_pipeline")
    with tracer.start_as_current_span(name, attributes=_clean({"langfuse.observation.type": kind, **attrs})) as s:
        yield Span(s)


@contextmanager
def model_span(operation: str, model: str, prompt_id: str, **attrs: Any) -> Iterator[Span]:
    """A model call: a Langfuse generation named "<prompt> <model>" (Q3 gpt-6-sol), or an embedding named
    "embeddings <model>", with GenAI attributes."""
    kind = "embedding" if operation == "embeddings" else "generation"
    with span(f"{operation if kind == 'embedding' else prompt_id} {model}", kind, **{"gen_ai.operation.name": operation, "gen_ai.system": "az.ai.openai",
                                               "gen_ai.request.model": model, "tmp.prompt_id": prompt_id,
                                               "langfuse.observation.metadata.prompt_id": prompt_id, **attrs}) as s:
        yield s
