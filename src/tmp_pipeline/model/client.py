"""Model client interface and offline clients.

A ``ModelRequest`` is everything that determines an answer (prompt id and version, model, text, image digests,
parameters), so its ``key()`` is a correct cache key: change the prompt version or the model and the cache
misses. ``CachedClient`` puts a FileCache in front of any client, ``FakeClient`` answers from canned data in
tests, and ``RecordingClient`` / ``ReplayClient`` capture live answers once and replay them offline.
Handlers call ``ask()``, which returns None (and logs ``model_skipped``) when no model is available, so a
handler can always fall back to marking the value unverified.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import logging_ as lg
from .. import tracing
from ..cache import CacheMiss, FileCache, stable_key
from ..errors import PipelineError


class ModelUnavailableError(PipelineError):
    """No model can answer this request (disabled, or nothing recorded for it)."""


@dataclass(frozen=True)
class ModelRequest:
    prompt_id: str                      # X2, X3, X4, X5, X6 ...
    prompt_version: str
    model: str
    user: str
    system: str = ""
    images: tuple[bytes, ...] = ()
    params: dict = field(default_factory=dict)

    def key(self) -> str:
        digests = [hashlib.sha256(b).hexdigest() for b in self.images]
        return stable_key(self.prompt_id, self.prompt_version, self.model, self.system, self.user, digests,
                          self.params)


@dataclass
class ModelResponse:
    data: dict                          # the parsed JSON reply
    model: str = ""
    cached: bool = False
    usage: dict = field(default_factory=dict)


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
