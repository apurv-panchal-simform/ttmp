"""tmp-pipeline-chunks <doc_dir>: rebuild chunks.jsonl and lineage.json from a finished run's saved results (plan 6.2:
a chunker change or a reviewer correction), without reading the PDF. Unchanged chunks keep their summaries and
vectors; the command prints which chunk ids were added, removed or changed, and which of them carry a value a
reviewer corrected (from corrections.jsonl)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ... import logging_ as lg
from ... import tracing
from ...config import ConfigError, load_config
from ...errors import PipelineError
from ...llm import MODES, model_client
from ...records import RecordStore
from ..embedding.embeddings import ReplayEmbeddings
from ..facts.model import Facts
from ..models import ElementStore, SchemaError, atomic_write_text
from .model import ChunkSet
from .stage import build_chunks, summarise_and_embed


def diff(old: ChunkSet | None, new: ChunkSet) -> dict[str, list[str]]:
    before = {c.chunk_id: c.content_hash for c in old.chunks} if old else {}
    after = {c.chunk_id: c.content_hash for c in new.chunks}
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "changed": sorted(k for k in set(after) & set(before) if after[k] != before[k])}


def corrected_chunks(doc_dir: Path, chunks: ChunkSet) -> list[str]:
    log = doc_dir / "corrections.jsonl"
    if not log.exists():
        return []
    paths = {p for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
             for p in json.loads(line).get("affected", [])}
    return sorted(c.chunk_id for c in chunks.chunks if paths & set(c.field_paths)
                  or paths & set(c.metadata.get("conflict_ids", [])))


def rebuild(doc_dir: str | Path, template: str | Path | None = None, model=None, embedder=None) -> tuple[ChunkSet, dict, dict]:
    d = Path(doc_dir)
    cfg = load_config(template)
    previous = ChunkSet.load_jsonl(d / "chunks.jsonl") if (d / "chunks.jsonl").exists() else None
    lg.start_run(d / "chunks.log")
    try:
        store = ElementStore.load(d / "elements.json")
        lg.set_doc_id(store.doc_id)
        with lg.stage("build_chunks"):
            chunks, lineage = build_chunks(Facts.load(d / "facts.json"), RecordStore.load(d / "records.json"), store, cfg)
        with lg.stage("summarise_and_embed"):
            summarise_and_embed(chunks, cfg, model, embedder, d, previous)
            chunks.dump_jsonl(d / "chunks.jsonl")
            atomic_write_text(d / "lineage.json", json.dumps(lineage, indent=1, ensure_ascii=False))
        changes = diff(previous, chunks)
        changes["corrected"] = corrected_chunks(d, chunks)
        lg.log_event("build_chunks", "changes", **{k: len(v) for k, v in changes.items()})
        return chunks, lineage, changes
    finally:
        lg.end_run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-chunks", description="Rebuild chunks.jsonl from saved results")
    ap.add_argument("doc_dir")
    ap.add_argument("--template", default=None)
    ap.add_argument("--model", choices=MODES, default="off")
    ap.add_argument("--model-fixtures", default=None)
    ap.add_argument("--embed", choices=("off", "replay"), default="off")
    ap.add_argument("--embed-fixtures", default=None)
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    if (a.model == "replay" and not a.model_fixtures) or (a.embed == "replay" and not a.embed_fixtures):
        ap.error("replay needs its fixtures folder")
    try:
        chunks, lineage, changes = rebuild(a.doc_dir, a.template, model_client(a.model, a.model_fixtures),
                                           ReplayEmbeddings(a.embed_fixtures) if a.embed == "replay" else None)
    except (PipelineError, ConfigError, SchemaError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{len(chunks.chunks)} chunks, lineage {'complete' if lineage['complete'] else 'with gaps'}; "
          + ", ".join(f"{k} {len(v)}" for k, v in changes.items()))
    for k in ("changed", "added", "removed", "corrected"):
        for cid in changes[k][:20]:
            print(f"  {k}: {cid}")
    return 0 if lineage["complete"] else 1
