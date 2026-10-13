"""Azure OpenAI embeddings (text-embedding-3-large, 3,072 dimensions) as an ``EmbeddingClient``.

    AZURE_OPENAI_ENDPOINT   https://<resource>.openai.azure.com
    AZURE_OPENAI_KEY        api key (never logged)
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT   deployment name (default text-embedding-3-large)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .. import logging_ as lg
from ..transport import ServiceError, Transport, raise_for_status, urllib_transport, with_retry

API_VERSION = "2024-10-21"


@dataclass(frozen=True)
class OpenAIConfig:
    endpoint: str
    key: str
    deployment: str = "text-embedding-3-large"
    dims: int = 3072

    @classmethod
    def from_env(cls) -> OpenAIConfig:
        endpoint, key = os.environ.get("AZURE_OPENAI_ENDPOINT", ""), os.environ.get("AZURE_OPENAI_KEY", "")
        if not endpoint or not key:
            raise ServiceError("set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY for live embeddings")
        return cls(endpoint.rstrip("/"), key, os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
                                                             "text-embedding-3-large"))


class AzureEmbeddings:
    model = "text-embedding-3-large"

    def __init__(self, config: OpenAIConfig | None = None, transport: Transport | None = None, batch: int = 16):
        self.config = config or OpenAIConfig.from_env()
        self.transport = transport or with_retry(urllib_transport)
        self.batch = batch
        self.dims = self.config.dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        url = (f"{self.config.endpoint}/openai/deployments/{self.config.deployment}/embeddings"
               f"?api-version={API_VERSION}")
        headers = {"api-key": self.config.key, "Content-Type": "application/json"}
        for i in range(0, len(texts), self.batch):
            chunk = texts[i:i + self.batch]
            body = json.dumps({"input": chunk, "dimensions": self.dims}).encode("utf-8")
            status, _, data = self.transport("POST", url, headers, body)
            raise_for_status(status, "embeddings", data)
            items = sorted(json.loads(data)["data"], key=lambda d: d["index"])
            if len(items) != len(chunk):
                raise ServiceError(f"embeddings: asked for {len(chunk)} vectors, got {len(items)}")
            out += [list(d["embedding"]) for d in items]
            lg.log_event("embed", "called", texts=len(chunk), deployment=self.config.deployment)
        return out
