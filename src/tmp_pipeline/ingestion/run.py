"""Entry point: intake -> layers -> elements -> zones -> furniture -> masters -> diagram stage -> router -> facts -> chunks.

    python -m tmp_pipeline.ingestion.run <pdf> --out out/ [--template file.toml] [--force] [--skip-render]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from .. import logging_ as lg
from .. import tracing
from ..config import ConfigError, load_config
from ..constants import MASTER_DPI
from ..errors import PipelineError
from ..llm import MODES, model_client, set_parallel
from ..records import RecordStore
from .catalog import load_catalog
from .chunking.model import ChunkSet
from .chunking.stage import build_chunks, summarise_and_embed
from .contracts import HandlerRegistry
from .embedding.embeddings import EmbeddingClient, ReplayEmbeddings
from .extract.diagrams.stage import read_diagrams
from .extract.furniture import apply_furniture
from .extract.handlers import build_registry
from .extract.intake import IntakeResult, compute_doc_id, doc_dir, intake, mark_done
from .extract.layers.reader import read_layers
from .extract.layers.render import Renderer
from .extract.layers.to_elements import build_elements
from .extract.router import RouterResult, route
from .extract.zones import assign_zones
from .facts import Facts, map_fields, run_checks
from .facts.model import review_json
from .models import ElementStore, SchemaError, atomic_write_text
from .report import build_report
from .score import render as render_gold
from .score import score as score_gold
from .versions import version_stamp


@dataclass
class RunResult:
    intake: IntakeResult
    out_dir: Path
    skipped: bool
    store: ElementStore | None = None
    router: RouterResult | None = None
    records: RecordStore | None = None
    facts: Facts | None = None
    chunks: ChunkSet | None = None
    lineage: dict | None = None


FAILED_MARKER = "FAILED"


def _write_failure(out: Path, stage: str, exc: BaseException, store: ElementStore | None) -> None:
    """Leave evidence of a failed run next to the logs: a FAILED marker and whatever was built so far."""
    atomic_write_text(out / FAILED_MARKER, json.dumps(
        {"stage": stage, "error": f"{type(exc).__name__}: {exc}", "run_id": lg.get_run_id(),
         "trace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}, indent=1))
    if store is not None:
        store.dump(out / "elements.partial.json")


def run(pdf_path: str | Path, out_root: str | Path, template: str | Path | None = None,
        force: bool = False, render: bool = True, registry: HandlerRegistry | None = None,
        model=None, gold: str | Path | None = None, embedder: EmbeddingClient | None = None) -> RunResult:
    out_root = Path(out_root)
    cfg = load_config(template)
    doc_id = compute_doc_id(pdf_path)                    # the hash names the per-document folder and its log
    out = doc_dir(out_root, doc_id)
    out.mkdir(parents=True, exist_ok=True)
    run_id = lg.start_run(out / "run.log")
    store: ElementStore | None = None
    current = "intake"
    try:
        lg.set_doc_id(doc_id)
        with lg.stage("intake"):
            info = intake(pdf_path, out_root, cfg.intake)
        if info.already_processed and not force:
            lg.log_event("run", "skipped", reason="already processed", doc_dir=str(out))
            return RunResult(info, out, True)
        (out / FAILED_MARKER).unlink(missing_ok=True)
        lg.log_event("run", "start", file=Path(pdf_path).name, versions=version_stamp(), template=cfg.template,
                     config_hash=cfg.config_hash(), scanned=info.scanned, version=info.version)
        current = "layers"
        with lg.stage("layers"):
            layers = read_layers(pdf_path)
            lg.log_event("layers", "read", pages=len(layers), chars=sum(len(p.chars) for p in layers))
        current = "elements"
        with lg.stage("elements"):
            store = build_elements(layers, info.doc_id, cfg)
        current = "zones"
        with lg.stage("zones"):
            assign_zones(store, cfg)
        current = "furniture"
        with lg.stage("furniture"):
            apply_furniture(store, cfg)
        if render:                                       # masters first: the diagram stage and X8 use them
            current = "render"
            with lg.stage("render"), Renderer(pdf_path) as r:
                for page in range(1, info.pages + 1):
                    path, size = r.save_master(page, out / "master")
                    store.pages[page].master = {"file": f"master/{path.name}", "px": list(size), "dpi": MASTER_DPI}
        current = "diagrams"
        with lg.stage("diagrams"):
            sheets = read_diagrams(store, pdf_path, cfg, out, render)
        store.ensure_pixel_boxes()
        store.metadata.update(file=Path(pdf_path).name, version=info.version, scanned=info.scanned,
                              pages=info.pages, run_id=run_id, versions=version_stamp(),
                              template=cfg.template, config_hash=cfg.config_hash())
        records = RecordStore(info.doc_id)
        current = "router"
        with lg.stage("router"):
            result = route(store, cfg, out / "router.log", registry or build_registry(), records, model, sheets=sheets, out_dir=out)
        catalog = load_catalog()
        current = "map_fields"
        with lg.stage("map_fields"):
            facts = map_fields(store, records, cfg, catalog, model)
        current = "run_checks"
        with lg.stage("run_checks"):
            run_checks(facts, store, records, cfg, catalog)
        current = "outputs"
        store.dump(out / "elements.json")
        records.dump(out / "records.json")
        if len(sheets):
            sheets.dump(out / "sheets.json")
        current = "write_facts"
        with lg.stage("write_facts"):
            facts.dump(out / "facts.json")
            atomic_write_text(out / "review.json", review_json(facts))
        current = "build_chunks"
        with lg.stage("build_chunks"):
            previous = ChunkSet.load_jsonl(out / "chunks.jsonl") if (out / "chunks.jsonl").exists() else None
            chunks, lineage = build_chunks(facts, records, store, cfg)
        current = "summarise_and_embed"
        with lg.stage("summarise_and_embed"):
            summarise_and_embed(chunks, cfg, model, embedder, out, previous)
            chunks.dump_jsonl(out / "chunks.jsonl")
            atomic_write_text(out / "lineage.json", json.dumps(lineage, indent=1, ensure_ascii=False))
        current = "outputs"
        gold_text = None
        if gold is not None:
            gold_score = score_gold(records, store, json.loads(Path(gold).read_text(encoding="utf-8")), facts=facts,
                                    chunks=chunks, lineage=lineage)
            gold_text = render_gold(gold_score)
            lg.log_event("gold", "scored", accuracy=gold_score.accuracy, wrong_verified=len(gold_score.wrong_verified),
                         invented=len(gold_score.invented), coverage=gold_score.coverage, passes=gold_score.passes())
        atomic_write_text(out / "report.md", build_report(store, result, info, records, gold_text, facts,
                                                                chunks, lineage))
        mark_done(out_root, info.doc_id)
        lg.log_event("run", "done", elements=len(store.elements), records=len(records),
                     coverage=result.coverage(store)["coverage"], failed=len(result.failures),
                     conflicts=len(facts.conflicts), approval=facts.approval.status if facts.approval else None,
                     chunks=len(chunks.chunks), lineage=lineage["complete"])
        return RunResult(info, out, False, store, result, records, facts, chunks, lineage)
    except BaseException as exc:
        _write_failure(out, current, exc, store)
        raise
    finally:
        lg.end_run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline")
    ap.add_argument("pdf")
    ap.add_argument("--out", default="out")
    ap.add_argument("--template", default=None)
    ap.add_argument("--force", action="store_true", help="re-run even if this file was already processed")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--model", choices=MODES, default="off",
                    help="off: no model calls (default); replay: recorded answers, offline; live: Azure OpenAI "
                         "(AZURE_OPENAI_ENDPOINT/KEY), recorded to --model-fixtures when given")
    ap.add_argument("--model-fixtures", default=None, help="replay: recorded answers; live: where to record them")
    ap.add_argument("--max-parallel", type=int, default=None, metavar="N",
                    help="live: calls in flight at once to each model deployment (default: TMP_MAX_PARALLEL or 2, sized to "
                         "the 30k TPM of gpt-6-luna; 1 = one after another)")
    ap.add_argument("--gold", default=None, help="gold JSON file: score the run and add the result to report.md")
    ap.add_argument("--embed", choices=("off", "replay"), default="off",
                    help="off: chunks without vectors (default); replay: vectors from recorded fixtures, offline")
    ap.add_argument("--embed-fixtures", default=None, help="folder of recorded vectors for --embed replay")
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    if a.model == "replay" and not a.model_fixtures:
        ap.error("--model replay needs --model-fixtures")
    if a.embed == "replay" and not a.embed_fixtures:
        ap.error("--embed replay needs --embed-fixtures")
    embedder = ReplayEmbeddings(a.embed_fixtures) if a.embed == "replay" else None
    if a.max_parallel is not None:
        if a.max_parallel < 1:
            ap.error("--max-parallel must be at least 1")
        set_parallel(a.max_parallel)
    try:
        model = model_client(a.model, a.model_fixtures, Path(a.out) / "_model_cache" if a.model == "live" else None)
        res = run(a.pdf, a.out, a.template, a.force, not a.skip_render, model=model, gold=a.gold, embedder=embedder)
    except (PipelineError, ConfigError, SchemaError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if res.skipped:
        print(f"already processed ({res.intake.doc_id[:19]}…); use --force to re-run: {res.out_dir}")
        return 0
    if res.router is None or res.store is None:
        raise RuntimeError("run finished without a router result")
    cov = res.router.coverage(res.store)
    print(f"{len(res.store.elements)} elements, coverage {cov['coverage']:.0%}, failed handlers {cov['failed']} "
          f"-> {res.out_dir}")
    return 0 if cov["coverage"] == 1.0 and not cov["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
