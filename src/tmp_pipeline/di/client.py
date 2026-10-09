"""Document Intelligence client: prebuilt-layout over REST (plan section 1.3).

API 2024-11-30 with add-ons ocrHighResolution and styleFont, and output=figures. A cache sits in front of
the service, and `cache_only` makes an offline run fail loudly instead of calling out. The key is read from
the environment and is never logged. The HTTP layer is a plain callable so tests run without a network.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import logging_ as lg
from .cache import CacheMiss, DiCache, cache_key

API_VERSION = "2024-11-30"
MODEL = "prebuilt-layout"
FEATURES = ["ocrHighResolution", "styleFont"]
OUTPUT = ["figures"]


@dataclass
class DiConfig:
    endpoint: str
    key: str | None = None            # None -> bearer token from azure-identity, if installed

    @classmethod
    def from_env(cls) -> "DiConfig":
        endpoint = os.environ.get("AZURE_DI_ENDPOINT")
        if not endpoint:
            raise RuntimeError("AZURE_DI_ENDPOINT is not set")
        return cls(endpoint.rstrip("/"), os.environ.get("AZURE_DI_KEY"))

    def headers(self) -> dict[str, str]:
        if self.key:
            return {"Ocp-Apim-Subscription-Key": self.key}
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as e:
            raise RuntimeError("no AZURE_DI_KEY set and azure-identity is not installed") from e
        token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
        return {"Authorization": f"Bearer {token}"}


Transport = "callable(method, url, headers, body) -> (status, response_headers, body_bytes)"


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:      # noqa: S310 (https endpoint from config)
        return r.status, dict(r.headers), r.read()


def analyze(pdf_path: str | Path, file_sha256: str, cache: DiCache, config: DiConfig | None = None, *,
            pages: str | None = None, cache_only: bool = False, transport=None, poll_s: float = 2.0,
            max_polls: int = 150) -> dict:
    """Return the analyzeResult dict for the PDF, from cache when possible."""
    key = cache_key(file_sha256, MODEL, API_VERSION, FEATURES + OUTPUT, pages)
    if cache.has(key):
        result = cache.get(key)
        lg.log_event("di", "response", cache_hit=True, cache_key=key, pages=len(result.get("pages", [])))
        return result
    if cache_only:
        lg.log_event("di", "cache_miss", cache_key=key, cache_only=True)
        raise CacheMiss(key)
    config = config or DiConfig.from_env()
    transport = transport or _urllib_transport
    body = Path(pdf_path).read_bytes()
    url = (f"{config.endpoint}/documentintelligence/documentModels/{MODEL}:analyze?api-version={API_VERSION}"
           f"&features={','.join(FEATURES)}&output={','.join(OUTPUT)}" + (f"&pages={pages}" if pages else ""))
    lg.log_event("di", "request", url=url.split("?")[0], features=FEATURES, output=OUTPUT, pages=pages or "all",
                 file_bytes=len(body))
    t0 = time.time()
    status, headers, _ = transport("POST", url, {**config.headers(), "Content-Type": "application/pdf"}, body)
    op = {k.lower(): v for k, v in headers.items()}.get("operation-location")
    if status not in (200, 202) or not op:
        raise RuntimeError(f"DI analyze request failed: HTTP {status}")
    for _ in range(max_polls):
        status, _, raw = transport("GET", op, config.headers(), None)
        doc = json.loads(raw)
        if doc.get("status") == "succeeded":
            result = doc["analyzeResult"]
            result["_resultId"] = op.rsplit("/analyzeResults/", 1)[-1].split("?")[0]
            cache.put(key, result)
            lg.log_event("di", "response", cache_hit=False, cache_key=key, latency_ms=round((time.time() - t0) * 1000),
                         response_bytes=len(raw), pages=len(result.get("pages", [])),
                         words=sum(len(p.get("words", [])) for p in result.get("pages", [])),
                         figures=len(result.get("figures", [])), tables=len(result.get("tables", [])))
            return result
        if doc.get("status") == "failed":
            raise RuntimeError(f"DI analysis failed: {doc.get('error')}")
        time.sleep(poll_s)
    raise TimeoutError("DI analysis did not finish in time")
