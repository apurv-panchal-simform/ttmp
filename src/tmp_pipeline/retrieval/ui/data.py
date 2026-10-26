"""Read-only loaders for a run folder, cached on (path, mtime) so a re-run shows up without a restart."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from PIL import Image

DOC_FILES = ("elements.json", "records.json", "sheets.json", "facts.json", "review.json", "lineage.json",
             "chunks.jsonl", "report.md", "run.log", "router.log", "facts.log", "chunks.log", "di.log",
             "di_triggers.json", "di_elements.json", "corrections.jsonl", "FAILED", "elements.partial.json")


@dataclass(frozen=True)
class DocEntry:
    path: Path
    status: str                         # done | failed | partial
    tmp_ref: str
    pages: int

    @property
    def label(self) -> str:
        return f"{self.tmp_ref} · {self.pages} pages · {self.status} · {self.path.name[:19]}…"


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner=False)
def _json(path: str, mtime: float) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def _jsonl(path: str, mtime: float) -> list[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"raw": line})
    return out


def read_json(path: Path) -> Any:
    return _json(str(path), _mtime(path)) if path.exists() else None


def read_jsonl(path: Path) -> list[dict]:
    return _jsonl(str(path), _mtime(path)) if path.exists() else []


def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def list_docs(root: Path) -> list[DocEntry]:
    """Every document folder under an output root (``sha256-*``), or the root itself when it is one."""
    if not root.is_dir():
        return []
    dirs = [root] if (root / "elements.json").exists() else sorted(
        p for p in root.iterdir() if p.is_dir() and p.name.startswith("sha256-"))
    out = []
    for d in dirs:
        meta = (read_json(d / "elements.json") or read_json(d / "elements.partial.json") or {}).get("metadata", {})
        status = "failed" if (d / "FAILED").exists() else "done" if (d / ".done").exists() else "partial"
        out.append(DocEntry(d, status, meta.get("tmp_reference") or "(no reference)", meta.get("pages") or 0))
    return out


def elements(doc: Path) -> dict:
    return read_json(doc / "elements.json") or read_json(doc / "elements.partial.json") or {}


def elements_df(doc: Path) -> pd.DataFrame:
    """Elements joined with their router decision (route, rule, outcome, claimed_by)."""
    els = elements(doc).get("elements", [])
    router = {r.get("element"): r for r in read_jsonl(doc / "router.log")}
    rows = []
    for e in els:
        r = router.get(e["element_id"], {})
        rows.append({"element_id": e["element_id"], "page": e["page"], "zone": e.get("zone"), "type": e.get("type"),
                     "route": r.get("route") or e.get("route"), "rule": r.get("rule"),
                     "outcome": r.get("outcome") or e.get("outcome"), "claimed_by": r.get("claimed_by"),
                     "flags": ", ".join(e.get("flags") or []), "text": _element_text(e),
                     "bbox_px": e.get("bbox_px"), "bbox_pt": e.get("bbox_pt")})
    return pd.DataFrame(rows)


def _element_text(e: dict) -> str:
    for key in ("text", "content"):
        if isinstance(e.get(key), str):
            return e[key][:200]
    reps = e.get("representations") or {}
    for v in reps.values():
        if isinstance(v, str):
            return v[:200]
    return ""


def records(doc: Path) -> list[dict]:
    data = read_json(doc / "records.json") or {}
    return data.get("records", []) if isinstance(data, dict) else data


def records_with_ids(doc: Path) -> list[dict]:
    """records.json omits the id; rebuild it the way RecordStore.add does (r<page>:<kind>:<n>, in file order)."""
    seen: dict[tuple, int] = {}
    out = []
    for r in records(doc):
        key = (r.get("page"), r.get("kind"))
        n = seen.get(key, 0)
        seen[key] = n + 1
        out.append({**r, "_rid": f"r{r.get('page')}:{r.get('kind')}:{n}"})
    return out


def sheets(doc: Path) -> list[dict]:
    data = read_json(doc / "sheets.json") or {}
    return data.get("sheets", []) if isinstance(data, dict) else data


def facts(doc: Path) -> dict:
    return read_json(doc / "facts.json") or {}


def chunks(doc: Path) -> tuple[dict, list[dict]]:
    rows = read_jsonl(doc / "chunks.jsonl")
    if rows and "chunk_id" not in rows[0]:
        return rows[0], rows[1:]
    return {}, rows


def chunk_index(doc: Path) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """element id -> ids of the chunks built from it, and chunk id -> chunk. A chunk's own element ids are used;
    a chunk that lists only records gets the elements of those records' evidence."""
    _, rows = chunks(doc)
    recs = {r["_rid"]: r for r in records_with_ids(doc)}
    by_element: dict[str, list[str]] = {}
    for c in rows:
        eids = set(c.get("element_ids") or [])
        if not eids:
            eids = {ev["element_id"] for rid in c.get("record_ids") or [] for ev in (recs.get(rid) or {}).get("evidence") or []}
        for e in eids:
            by_element.setdefault(e, []).append(c["chunk_id"])
    return by_element, {c["chunk_id"]: c for c in rows}


def page_info(doc: Path, page: int) -> dict:
    return (elements(doc).get("pages") or {}).get(str(page), {})


@st.cache_data(show_spinner=False, max_entries=8)
def _image(path: str, mtime: float) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def image(path: Path) -> Image.Image | None:
    return _image(str(path), _mtime(path)) if path.exists() else None


def master(doc: Path, page: int) -> Image.Image | None:
    return image(doc / "master" / f"page{page:02d}_master.webp")


def overlay(doc: Path, page: int) -> Image.Image | None:
    return image(doc / "master" / f"page{page:02d}_overlay.webp")


def last_run(events: list[dict]) -> list[dict]:
    """run.log is appended to by every run; keep the events of the latest run id."""
    ids = [e.get("run_id") for e in events if e.get("run_id")]
    return [e for e in events if e.get("run_id") == ids[-1]] if ids else events


def stage_timings(events: list[dict]) -> pd.DataFrame:
    rows = [{"stage": e.get("stage"), "duration_ms": e.get("duration_ms", 0)}
            for e in last_run(events) if e.get("event") == "stage_finished"]
    return pd.DataFrame(rows)


def root_files(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern)) if root.is_dir() else []
