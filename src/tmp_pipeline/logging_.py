"""JSON-lines run logger. Every line carries run_id and doc_id (plan sections 1.1, 1.6)."""
from __future__ import annotations

import contextvars
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
_doc_id: contextvars.ContextVar[str] = contextvars.ContextVar("doc_id", default="")
_sink = None
_console = False
_SECRET_KEYS = {"api_key", "key", "token", "secret", "password", "authorization"}


def start_run(path: str | Path, console: bool = False) -> str:
    """Open the log file and return a new run id (r-YYYYmmdd-HHMMSS-xxxx)."""
    global _sink, _console
    end_run()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _sink = p.open("a", encoding="utf-8")
    _console = console
    run_id = f"r-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    _run_id.set(run_id)
    _doc_id.set("")
    return run_id


def set_doc_id(doc_id: str) -> None:
    _doc_id.set(doc_id)


def get_run_id() -> str:
    return _run_id.get()


def get_doc_id() -> str:
    return _doc_id.get()


def end_run() -> None:
    global _sink
    if _sink is not None:
        _sink.close()
        _sink = None
    _run_id.set("")


def log_event(stage: str, event: str, **fields) -> None:
    if _sink is None:
        return
    rec = {"ts": round(time.time(), 3), "run_id": _run_id.get(), "doc_id": _doc_id.get(),
           "stage": stage, "event": event}
    rec.update({k: ("<redacted>" if k.lower() in _SECRET_KEYS else v) for k, v in fields.items()})
    _sink.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    _sink.flush()
    if _console:
        print(f"[{stage}] {event} " + " ".join(f"{k}={v}" for k, v in fields.items()))
