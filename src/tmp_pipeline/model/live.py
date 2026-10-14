"""One place that turns ``--model off|replay|live`` into a model client, for every CLI."""
from __future__ import annotations

from pathlib import Path

from ..cache import FileCache
from .client import CachedClient, ModelClient, RecordingClient, ReplayClient

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
        from .azure_chat import AzureChat, warm
        client: ModelClient = AzureChat()
        warm()
        if fixtures:
            client = RecordingClient(client, fixtures)
        if cache_dir:
            client = CachedClient(client, FileCache(cache_dir))
        return client
    raise ValueError(f"unknown model mode {mode!r} (use one of {', '.join(MODES)})")
