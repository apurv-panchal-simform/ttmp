"""Live Azure OpenAI chat client (chat completions, JSON output, temperature 0) as a ``ModelClient``.

    AZURE_OPENAI_ENDPOINT          https://<resource>.openai.azure.com
    AZURE_OPENAI_KEY               api key (never logged)
    AZURE_OPENAI_CHAT_DEPLOYMENTS  optional "model=deployment,model=deployment" when deployment names differ from the
                                   model names in ``prompts.MODELS`` (for example a Data Zone deployment, plan 8.2)

A service failure is logged and raised as ``ModelUnavailableError``, so callers fall back exactly as they do with no
model (``ask`` returns None and the value stays unverified, or the answer becomes the safe answer). Wrap this client
in ``CachedClient`` and ``RecordingClient`` (see ``model.live``) so every live answer can be replayed offline.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass, field

from .. import logging_ as lg
from .. import tracing
from ..transport import ServiceError, Transport, raise_for_status, shared_keepalive, with_retry
from . import pricing
from .client import ModelRequest, ModelResponse, ModelUnavailableError

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
        self.transport = transport or with_retry(shared_keepalive())
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

    def complete(self, request: ModelRequest) -> ModelResponse:
        deployment = self.config.deployment(request.model)
        endpoint, key = self.config.target(request.model)
        url = (f"{endpoint}/openai/deployments/{deployment}/chat/completions"
               f"?api-version={API_VERSION}")
        body: dict = {"messages": messages(request), "response_format": {"type": "json_object"}}
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
                status, data = self._post(url, headers, body, deployment)
                sp.set(**{"tmp.settings_refused": sorted(self.unsupported.get(deployment, ())),
                          "gen_ai.request.reasoning_effort": None if "reasoning_effort" in self.unsupported.get(
                              deployment, ()) else effort or None})
                raise_for_status(status, f"chat {request.prompt_id}", data)
                reply = json.loads(data)
                text = reply["choices"][0]["message"]["content"] or ""
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
                     pricing.cost(request.model, usage.get("prompt_tokens"), usage.get("completion_tokens")))
            sp.output(text)
        lg.log_event("model", "live_call", prompt=request.prompt_id, deployment=deployment,
                     prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        return ModelResponse(data=parsed, model=reply.get("model") or request.model, usage=usage)
