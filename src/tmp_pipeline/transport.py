"""HTTP transport shared by the DI client and any model client: a plain callable (so tests run offline),
retry with backoff for throttling and server errors, and typed errors instead of raw urllib exceptions."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .errors import PipelineError

# (method, url, headers, body) -> (status, response headers, body bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ServiceError(PipelineError):
    """A remote service refused or failed a request."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransientServiceError(ServiceError):
    """Worth retrying: throttling, server errors, network failure."""


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float = 120) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (https endpoint from config)
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:                              # an HTTP status is a response, not an exception
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise TransientServiceError(f"{method} {url.split('?')[0]}: {type(e).__name__}: {e}") from e


def with_retry(transport: Transport, *, retries: int = 3, backoff_s: float = 1.0, max_backoff_s: float = 30.0,
               sleep: Callable[[float], None] = time.sleep) -> Transport:
    """Wrap a transport so 408/429/5xx responses and transient errors are retried with exponential backoff
    (honouring Retry-After). The last response is returned after the final attempt; the caller decides."""
    def wrapped(method: str, url: str, headers: dict[str, str], body: bytes | None):
        for attempt in range(retries + 1):
            try:
                status, resp_headers, data = transport(method, url, headers, body)
            except TransientServiceError:
                if attempt == retries:
                    raise
                sleep(min(max_backoff_s, backoff_s * 2 ** attempt))
                continue
            if status not in RETRY_STATUSES or attempt == retries:
                return status, resp_headers, data
            retry_after = {k.lower(): v for k, v in resp_headers.items()}.get("retry-after", "")
            delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else backoff_s * 2 ** attempt
            sleep(min(max_backoff_s, delay))
        raise AssertionError("unreachable")                          # pragma: no cover
    return wrapped


def raise_for_status(status: int, what: str, body: bytes = b"") -> None:
    """Raise ServiceError (TransientServiceError for retryable statuses) unless the status is 2xx."""
    if 200 <= status < 300:
        return
    detail = body[:200].decode("utf-8", "replace")
    exc = TransientServiceError if status in RETRY_STATUSES else ServiceError
    raise exc(f"{what} failed: HTTP {status} {detail}".strip(), status)
