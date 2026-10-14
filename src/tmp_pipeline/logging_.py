"""JSON-lines run logger. Every line carries run_id and doc_id (plan sections 1.1, 1.6).

One ``RunLogger`` per run holds the sink, the ids and a lock, so concurrent writers cannot interleave a line.
The active logger lives in a ``ContextVar``; threads do not inherit it, so start work in a pool with
``submit_in_context`` (or run it under ``contextvars.copy_context().run``) to keep ``run_id`` and ``doc_id`` on
their lines. The module-level functions are the API every stage uses, and they do nothing without a run.
"""
from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

_SECRET_KEYS = {"api_key", "key", "token", "secret", "password", "authorization", "subscription_key"}


def _redact(value: Any) -> Any:
    """Redact secret-named keys at any depth."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if str(k).lower() in _SECRET_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class RunLogger:
    def __init__(self, path: str | Path, console: bool = False):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._sink: IO[str] | None = p.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self.console = console
        self.run_id = f"r-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self.doc_id = ""

    def write(self, level: str, stage: str, event: str, **fields: Any) -> None:
        rec = {"ts": round(time.time(), 3), "level": level, "run_id": self.run_id, "doc_id": self.doc_id,
               "stage": stage, "event": event, **_redact(fields)}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            if self._sink is None:
                return
            self._sink.write(line)
            self._sink.flush()
        if self.console:
            print(f"[{stage}] {event} " + " ".join(f"{k}={v}" for k, v in fields.items()))

    def close(self) -> None:
        with self._lock:
            if self._sink is not None:
                self._sink.close()
                self._sink = None


_current: contextvars.ContextVar[RunLogger | None] = contextvars.ContextVar("run_logger", default=None)


def start_run(path: str | Path, console: bool = False) -> str:
    """Open the log file and return a new run id (r-YYYYmmdd-HHMMSS-xxxx)."""
    end_run()
    logger = RunLogger(path, console)
    _current.set(logger)
    return logger.run_id


def end_run() -> None:
    logger = _current.get()
    if logger is not None:
        logger.close()
        _current.set(None)


def set_doc_id(doc_id: str) -> None:
    logger = _current.get()
    if logger is not None:
        logger.doc_id = doc_id


def get_run_id() -> str:
    logger = _current.get()
    return logger.run_id if logger else ""


def get_doc_id() -> str:
    logger = _current.get()
    return logger.doc_id if logger else ""


def log_event(stage: str, event: str, **fields: Any) -> None:
    logger = _current.get()
    if logger is not None:
        logger.write("info", stage, event, **fields)


def log_warning(stage: str, event: str, **fields: Any) -> None:
    logger = _current.get()
    if logger is not None:
        logger.write("warning", stage, event, **fields)


def log_exception(stage: str, event: str, exc: BaseException, **fields: Any) -> None:
    logger = _current.get()
    if logger is not None:
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.write("error", stage, event, error=f"{type(exc).__name__}: {exc}", trace=trace, **fields)


@contextmanager
def stage(name: str, **fields: Any) -> Iterator[None]:
    """Log stage_started / stage_finished (with duration), or stage_failed with the traceback, then re-raise.
    Each stage is also a tracing span (a no-op unless tracing is configured)."""
    from . import tracing
    log_event(name, "stage_started", **fields)
    t0 = time.perf_counter()
    with tracing.span(f"stage {name}", "chain", **{"tmp.doc_id": get_doc_id() or None, "tmp.run_id": get_run_id() or None}) as sp:
        try:
            yield
        except BaseException as exc:
            sp.error(exc)
            log_exception(name, "stage_failed", exc, duration_ms=round((time.perf_counter() - t0) * 1000))
            raise
    log_event(name, "stage_finished", duration_ms=round((time.perf_counter() - t0) * 1000))


def submit_in_context(executor: Executor, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """executor.submit that carries the current run's logger into the worker thread."""
    return executor.submit(contextvars.copy_context().run, lambda: fn(*args, **kwargs))
