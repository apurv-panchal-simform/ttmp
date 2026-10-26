"""Model clients: the interface, the offline clients, the live Azure OpenAI chat client and the call cost.

A ``ModelRequest`` is everything that determines an answer (prompt id and version, model, text, image digests,
parameters), so its ``key()`` is a correct cache key: change the prompt version or the model and the cache
misses. ``CachedClient`` puts a FileCache in front of any client, ``FakeClient`` answers from canned data in
tests, and ``RecordingClient`` / ``ReplayClient`` capture live answers once and replay them offline.
Callers use ``ask()``, which returns None (and logs ``model_skipped``) when no model is available, so a caller
can always fall back (an extracted value stays unverified, an answer becomes the code-built safe answer).

``AzureChat`` is the live chat client (chat completions, JSON output, temperature 0):

    AZURE_OPENAI_ENDPOINT          https://<resource>.openai.azure.com
    AZURE_OPENAI_KEY               api key (never logged)
    AZURE_OPENAI_CHAT_DEPLOYMENTS  optional "model=deployment,model=deployment" when deployment names differ from the
                                   model names in the prompts' MODELS (for example a Data Zone deployment)

A service failure is logged and raised as ``ModelUnavailableError``. ``model_client`` turns ``--model off|replay|live``
into a client for every CLI; live answers are recorded for offline replay and cached.

Prices are data (``data/model_prices.toml``, USD per 1M tokens); ``TMP_MODEL_PRICES`` points at another file of the
same shape. A model without a price gives no cost (usage is still traced).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import tomllib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from . import logging_ as lg
from . import tracing
from .cache import CacheMiss, FileCache, stable_key
from .errors import PipelineError
from .transport import ServiceError, Transport, raise_for_status, shared_keepalive, with_retry


class ModelUnavailableError(PipelineError):
    """No model can answer this request (disabled, or nothing recorded for it)."""


@dataclass(frozen=True)
class ModelRequest:
    prompt_id: str                      # X2, X4, X5, X6 ...
    prompt_version: str
    model: str
    user: str
    system: str = ""
    images: tuple[bytes, ...] = ()
    params: dict = field(default_factory=dict)
    tools: tuple[str, ...] = ()         # JSON of each tool the model may call (OpenAI "tools" schema)
    thread: tuple[str, ...] = ()        # JSON of the messages after the user text: tool calls it made and their results

    def key(self) -> str:
        digests = [hashlib.sha256(b).hexdigest() for b in self.images]
        return stable_key(self.prompt_id, self.prompt_version, self.model, self.system, self.user, digests,
                          self.params, self.tools, self.thread)


@dataclass
class ModelResponse:
    data: dict                          # the parsed JSON reply
    model: str = ""
    cached: bool = False
    usage: dict = field(default_factory=dict)


def build_request(prompt_id: str, version: str, model: str, system: str, user: str, images: tuple[bytes, ...] = (),
                  reasoning: str | None = None, tools: tuple[str, ...] = (), thread: tuple[str, ...] = ()) -> ModelRequest:
    """A request at temperature 0, with the reasoning effort when the prompt sets one."""
    params: dict = {"temperature": 0}
    if reasoning:
        params["reasoning_effort"] = reasoning
    return ModelRequest(prompt_id, version, model, user, system, images, params, tools, thread)


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class DisabledClient:
    def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelUnavailableError("model calls are disabled for this run")


class FakeClient:
    """Canned answers keyed by prompt id: a dict, or a callable(request) -> dict. Records every call."""

    def __init__(self, answers: dict[str, dict | Callable[[ModelRequest], dict]]):
        self.answers = answers
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        answer = self.answers.get(request.prompt_id)
        if answer is None:
            raise ModelUnavailableError(f"no canned answer for prompt {request.prompt_id}")
        data = answer(request) if callable(answer) else answer
        return ModelResponse(data=data, model=request.model)


class CachedClient:
    def __init__(self, inner: ModelClient, cache: FileCache):
        self.inner, self.cache = inner, cache

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = request.key()
        try:
            hit = self.cache.get(key)
        except CacheMiss:
            pass
        else:
            lg.log_event("model", "cache_hit", prompt=request.prompt_id, key=key)
            return ModelResponse(data=hit["data"], model=hit.get("model", request.model), cached=True,
                                 usage=hit.get("usage", {}))
        resp = self.inner.complete(request)
        self.cache.put(key, {"data": resp.data, "model": resp.model, "usage": resp.usage,
                             "prompt": request.prompt_id, "prompt_version": request.prompt_version})
        lg.log_event("model", "call", prompt=request.prompt_id, key=key, model=resp.model, usage=resp.usage)
        return resp


class RecordingClient:
    """Wraps a live client and saves each answer under its request key, for ReplayClient to serve later."""

    def __init__(self, inner: ModelClient, fixtures: str | Path):
        self.inner, self.fixtures = inner, FileCache(fixtures)

    def complete(self, request: ModelRequest) -> ModelResponse:
        resp = self.inner.complete(request)
        self.fixtures.put(request.key(), {"data": resp.data, "model": resp.model, "usage": resp.usage,
                                          "prompt": request.prompt_id})
        return resp


class ReplayClient:
    """Serves answers recorded by RecordingClient; an unrecorded request raises instead of calling out."""

    def __init__(self, fixtures: str | Path):
        self.fixtures = FileCache(fixtures)

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            hit: Any = self.fixtures.get(request.key())
        except CacheMiss:
            raise ModelUnavailableError(f"nothing recorded for {request.prompt_id} ({request.key()})") from None
        return ModelResponse(data=hit["data"], model=hit.get("model", request.model), usage=hit.get("usage", {}))


def ask(model: ModelClient | None, request: ModelRequest) -> dict | None:
    """The parsed answer, or None when there is no model or it cannot answer (the caller then marks unverified)."""
    with tracing.span(f"model {request.prompt_id}", **{"tmp.prompt_id": request.prompt_id,
                                                         "tmp.prompt_version": request.prompt_version,
                                                         "gen_ai.request.model": request.model}) as sp:
        if model is None:
            sp.set(**{"tmp.outcome": "skipped", "langfuse.observation.level": "WARNING",
                      "langfuse.observation.status_message": "no model configured (--model off): safe answer instead"})
            lg.log_event("model", "model_skipped", prompt=request.prompt_id, reason="no model configured")
            return None
        try:
            resp = model.complete(request)
        except ModelUnavailableError as e:
            sp.set(**{"tmp.outcome": "skipped", "tmp.reason": str(e)[:200], "langfuse.observation.level": "WARNING",
                      "langfuse.observation.status_message": str(e)[:200]})
            lg.log_event("model", "model_skipped", prompt=request.prompt_id, reason=str(e))
            return None
        sp.set(**{"tmp.outcome": "answered", "tmp.cached": resp.cached})
        return resp.data


# --- running calls side by side -----------------------------------------------------------------------------------
# Only the round-trip to the model moves to worker threads. Building requests, checking answers and creating records
# stay on the caller's thread, in the original order, so the output does not depend on the number of workers.

# The deployments in deploy/azure/models.tsv hold 20-50k TPM (gpt-6-luna, which does most of the image work, 30k).
# One call at a time already uses about 11k tokens a minute of luna's 30k, so 2 calls at once fill about 75% of it;
# more than 3 would only draw 429s. Raise it (TMP_MAX_PARALLEL / --max-parallel) after raising the quota.
DEFAULT_PARALLEL = 2


def _env_parallel() -> int:
    try:
        return max(1, int(os.environ.get("TMP_MAX_PARALLEL", DEFAULT_PARALLEL)))
    except ValueError:
        return DEFAULT_PARALLEL


_parallel = _env_parallel()


def set_parallel(n: int) -> None:
    """How many calls may be in flight at once to each model deployment (1 = one after another). Set before the
    client is built: the live client sizes its limiters from it."""
    global _parallel
    _parallel = max(1, int(n))


def parallel() -> int:
    return _parallel


def map_parallel[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int | None = None) -> list[R]:
    """``[fn(x) for x in items]`` with the calls running on worker threads; results come back in input order and the
    first failure (in input order) is raised. ``fn`` must not touch shared state. Each worker carries the run's
    logger and tracing context. Nested use is safe: the real limit is the live client's limiter."""
    items = list(items)
    n = min(workers if workers is not None else _parallel * 3, len(items))     # threads may wait on the limiter
    if n <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="tmp-model") as pool:
        futures = [lg.submit_in_context(pool, fn, x) for x in items]
        return [f.result() for f in futures]


def ask_many(model: ModelClient | None, requests: Sequence[ModelRequest], workers: int | None = None) -> list[dict | None]:
    """``ask`` for every request, in parallel, answers in request order. Identical requests are asked once."""
    keys = [r.key() for r in requests]
    first = {k: i for i, k in reversed(list(enumerate(keys)))}      # key -> index of its first request
    unique = sorted(first.values())
    answers = dict(zip((keys[i] for i in unique),
                       map_parallel(lambda i: ask(model, requests[i]), unique, workers), strict=True))
    return [answers[k] for k in keys]


# --- the live Azure OpenAI chat client ---------------------------------------------------------------------------

API_VERSION = "2025-04-01-preview"     # reasoning_effort needs it
OPTIONAL = ("temperature", "reasoning_effort")


@dataclass(frozen=True)
class ChatConfig:
    endpoint: str
    key: str
    deployments: dict[str, str] = field(default_factory=dict)    # model name -> deployment name
    reasoning_effort: str = ""          # none | low | medium | high: "" leaves the model's default (medium, slow)
    routes: dict[str, tuple[str, str]] = field(default_factory=dict)    # model -> (endpoint, key) of another resource

    @classmethod
    def from_env(cls) -> ChatConfig:
        """``AZURE_OPENAI_ROUTES='gpt-6-luna=LUNA'`` sends that model to ``AZURE_OPENAI_LUNA_ENDPOINT`` with
        ``AZURE_OPENAI_LUNA_KEY`` (a model deployed in another resource, e.g. when this one has no quota for it)."""
        endpoint, key = os.environ.get("AZURE_OPENAI_ENDPOINT", ""), os.environ.get("AZURE_OPENAI_KEY", "")
        if not endpoint or not key:
            raise ServiceError("set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY for live model calls")
        pairs = [p.split("=", 1) for p in os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENTS", "").split(",") if "=" in p]
        routes: dict[str, tuple[str, str]] = {}
        for m, name in (p.split("=", 1) for p in os.environ.get("AZURE_OPENAI_ROUTES", "").split(",") if "=" in p):
            prefix = f"AZURE_OPENAI_{name.strip().upper()}"
            r_end, r_key = os.environ.get(f"{prefix}_ENDPOINT", ""), os.environ.get(f"{prefix}_KEY", "")
            if not r_end or not r_key:
                raise ServiceError(f"AZURE_OPENAI_ROUTES sends {m.strip()} to {name.strip()}: set {prefix}_ENDPOINT "
                                   f"and {prefix}_KEY")
            routes[m.strip()] = (r_end.rstrip("/"), r_key)
        return cls(endpoint.rstrip("/"), key, {m.strip(): d.strip() for m, d in pairs},
                   os.environ.get("AZURE_OPENAI_REASONING_EFFORT", "low").strip(), routes)

    def deployment(self, model: str) -> str:
        return self.deployments.get(model, model)

    def target(self, model: str) -> tuple[str, str]:
        """(endpoint, key) serving ``model``."""
        return self.routes.get(model, (self.endpoint, self.key))


def messages(request: ModelRequest) -> list[dict]:
    """System text, then the user text with any images as data URLs."""
    out: list[dict] = []
    if request.system:
        out.append({"role": "system", "content": request.system})
    if request.images:
        parts: list[dict] = [{"type": "text", "text": request.user}]
        parts += [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                                             + base64.b64encode(img).decode("ascii")}}
                  for img in request.images]
        out.append({"role": "user", "content": parts})
    else:
        out.append({"role": "user", "content": request.user})
    out += [json.loads(m) for m in request.thread]
    return out


def warm() -> None:
    """Start the TLS handshake to Azure OpenAI in the background, so it overlaps start-up work (loading chunks,
    reading facts) instead of delaying the first embedding or answer by about 1.5 s."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    if endpoint.startswith("https://"):
        threading.Thread(target=shared_keepalive().warm, args=(endpoint,), daemon=True).start()


class AzureChat:
    def __init__(self, config: ChatConfig | None = None, transport: Transport | None = None):
        self.config = config or ChatConfig.from_env()
        # Calls run side by side, so a burst of 429s is likelier: more retries, spread out with jitter.
        self.transport = transport or with_retry(shared_keepalive(), retries=5, backoff_s=2.0, jitter=0.25)
        self._slots: dict[str, threading.BoundedSemaphore] = {}  # per model: each deployment has its own TPM quota
        self._slots_lock = threading.Lock()
        self.unsupported: dict[str, set[str]] = {}   # deployment -> optional settings it refused (temperature ...)

    def _post(self, url: str, headers: dict[str, str], body: dict, deployment: str) -> tuple[int, bytes]:
        """POST; an optional setting the model refuses (temperature on reasoning models, reasoning_effort on others)
        is dropped, the call made again, and the refusal remembered for that deployment."""
        refused = self.unsupported.setdefault(deployment, set())
        body = {k: v for k, v in body.items() if k not in refused}
        while True:
            status, _, data = self.transport("POST", url, headers, json.dumps(body).encode("utf-8"))
            bad = next((k for k in OPTIONAL if k in body and f'"{k}"'.encode() in data
                        and any(w in data for w in (b"nsupported", b"not supported", b"nrecognized"))), None) if status == 400 else None
            if bad is None:
                return status, data
            refused.add(bad)
            lg.log_warning("model", "setting_not_supported", deployment=deployment, setting=bad,
                           requested=body[bad], note="sent without it (model default)")
            body = {k: v for k, v in body.items() if k != bad}

    def _slot(self, model: str) -> threading.BoundedSemaphore:
        with self._slots_lock:
            if model not in self._slots:
                self._slots[model] = threading.BoundedSemaphore(parallel())
            return self._slots[model]

    def complete(self, request: ModelRequest) -> ModelResponse:
        deployment = self.config.deployment(request.model)
        endpoint, key = self.config.target(request.model)
        url = (f"{endpoint}/openai/deployments/{deployment}/chat/completions"
               f"?api-version={API_VERSION}")
        body: dict = {"messages": messages(request), "response_format": {"type": "json_object"}}
        if request.tools:
            body["tools"] = [json.loads(t) for t in request.tools]
            body["tool_choice"] = "auto"
        if "temperature" in request.params:
            body["temperature"] = request.params["temperature"]
        effort = request.params.get("reasoning_effort", self.config.reasoning_effort)
        if effort:
            body["reasoning_effort"] = effort
        headers = {"api-key": key, "Content-Type": "application/json"}
        with tracing.model_span("chat", request.model, request.prompt_id, **{
                "tmp.prompt_version": request.prompt_version, "gen_ai.request.temperature": body.get("temperature"),
                "server.address": endpoint.split("//", 1)[-1], "tmp.deployment": deployment,
                "tmp.images": len(request.images)}) as sp:
            sp.input([m if isinstance(m["content"], str) else {"role": m["role"], "content": request.user,
                                                                "images": len(request.images)}
                      for m in messages(request)])
            try:
                with self._slot(request.model):
                    status, data = self._post(url, headers, body, deployment)
                sp.set(**{"tmp.settings_refused": sorted(self.unsupported.get(deployment, ())),
                          "gen_ai.request.reasoning_effort": None if "reasoning_effort" in self.unsupported.get(
                              deployment, ()) else effort or None})
                raise_for_status(status, f"chat {request.prompt_id}", data)
                reply = json.loads(data)
                msg = reply["choices"][0]["message"]
                if msg.get("tool_calls"):         # the model asks for tools: {"tool_calls": [{id, name, arguments}]}
                    text = json.dumps({"tool_calls": [
                        {"id": c["id"], "name": c["function"]["name"],
                         "arguments": json.loads(c["function"].get("arguments") or "{}")} for c in msg["tool_calls"]]})
                else:
                    text = msg["content"] or ""
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise TypeError("the reply is not a JSON object")
            except (ServiceError, KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                sp.error(e)
                lg.log_warning("model", "live_call_failed", prompt=request.prompt_id, deployment=deployment,
                               error=f"{type(e).__name__}: {e}")
                raise ModelUnavailableError(f"{request.prompt_id}: live call failed ({type(e).__name__})") from e
            usage = reply.get("usage") or {}
            sp.set(**{"gen_ai.response.model": reply.get("model"), "gen_ai.response.id": reply.get("id"),
                      "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
                      "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
                      "gen_ai.response.finish_reasons": [str(reply["choices"][0].get("finish_reason") or "")]})
            sp.usage(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                     cost(request.model, usage.get("prompt_tokens"), usage.get("completion_tokens")))
            sp.output(text)
        lg.log_event("model", "live_call", prompt=request.prompt_id, deployment=deployment,
                     prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        return ModelResponse(data=parsed, model=reply.get("model") or request.model, usage=usage)


# --- cost --------------------------------------------------------------------------------------------------------

PRICES_FILE = "model_prices.toml"


@lru_cache(maxsize=4)
def load_prices(path: str = "") -> dict[str, dict[str, float]]:
    path = path or os.environ.get("TMP_MODEL_PRICES", "")
    text = (Path(path).read_text(encoding="utf-8") if path
            else resources.files("tmp_pipeline").joinpath("data", PRICES_FILE).read_text(encoding="utf-8"))
    return {name: {k: float(v) for k, v in p.items()} for name, p in tomllib.loads(text).get("models", {}).items()}


def cost(model: str, input_tokens: int | None, output_tokens: int | None = 0) -> dict[str, float] | None:
    """{"input", "output", "total"} in USD, or None when the model has no price or there is no usage."""
    price = load_prices().get(model)
    if price is None or input_tokens is None:
        return None
    cin = (input_tokens or 0) * price.get("input", 0.0) / 1e6
    cout = (output_tokens or 0) * price.get("output", 0.0) / 1e6
    return {"input": round(cin, 8), "output": round(cout, 8), "total": round(cin + cout, 8)}


# --- mode -> client ----------------------------------------------------------------------------------------------

MODES = ("off", "replay", "live")


def model_client(mode: str, fixtures: str | Path | None = None, cache_dir: str | Path | None = None) -> ModelClient | None:
    """off: None (no model calls). replay: answers recorded earlier, offline. live: Azure OpenAI, with every answer
    saved to ``fixtures`` (when given) for later replay and cached in ``cache_dir`` (when given)."""
    if mode == "off":
        return None
    if mode == "replay":
        if not fixtures:
            raise ValueError("--model replay needs a fixtures folder")
        return ReplayClient(fixtures)
    if mode == "live":
        client: ModelClient = AzureChat()
        warm()
        if fixtures:
            client = RecordingClient(client, fixtures)
        if cache_dir:
            client = CachedClient(client, FileCache(cache_dir))
        return client
    raise ValueError(f"unknown model mode {mode!r} (use one of {', '.join(MODES)})")
