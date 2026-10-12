"""Entry point: intake -> layers -> elements -> zones -> furniture -> masters -> diagram stage -> router -> facts.

    python -m tmp_pipeline.run <pdf> --out out/ [--template file.toml] [--force] [--skip-render]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import logging_ as lg
from .catalog import load_catalog
from .config import ConfigError, load_config
from .constants import MASTER_DPI
from .contracts import HandlerRegistry
from .diagrams.stage import read_diagrams
from .errors import PipelineError
from .facts import Facts, map_fields, run_checks
from .facts.model import review_json
from .furniture import apply_furniture
from .gold.score import render as render_gold
from .gold.score import score as score_gold
from .handlers import build_registry
from .intake import IntakeResult, compute_doc_id, doc_dir, intake, mark_done
from .layers.reader import read_layers
from .layers.render import Renderer
from .layers.to_elements import build_elements
from .model.client import ReplayClient
from .models import ElementStore, SchemaError, atomic_write_text
from .records import RecordStore
from .report import build_report
from .router import RouterResult, route
from .versions import version_stamp
from .zones import assign_zones


@dataclass
class RunResult:
    intake: IntakeResult
    out_dir: Path
    skipped: bool
    store: ElementStore | None = None
    router: RouterResult | None = None
    records: RecordStore | None = None
    facts: Facts | None = None


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
        model=None, gold: str | Path | None = None) -> RunResult:
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
        current = "outputs"
        gold_text = None
        if gold is not None:
            gold_score = score_gold(records, store, json.loads(Path(gold).read_text(encoding="utf-8")), facts=facts)
            gold_text = render_gold(gold_score)
            lg.log_event("gold", "scored", accuracy=gold_score.accuracy, wrong_verified=len(gold_score.wrong_verified),
                         invented=len(gold_score.invented), coverage=gold_score.coverage, passes=gold_score.passes())
        atomic_write_text(out / "report.md", build_report(store, result, info, records, gold_text, facts))
        mark_done(out_root, info.doc_id)
        lg.log_event("run", "done", elements=len(store.elements), records=len(records),
                     coverage=result.coverage(store)["coverage"], failed=len(result.failures),
                     conflicts=len(facts.conflicts), approval=facts.approval.status if facts.approval else None)
        return RunResult(info, out, False, store, result, records, facts)
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
    ap.add_argument("--model", choices=("off", "replay"), default="off",
                    help="off: no model calls (default); replay: answer X2-X10 from recorded fixtures, offline")
    ap.add_argument("--model-fixtures", default=None, help="folder of recorded answers for --model replay")
    ap.add_argument("--gold", default=None, help="gold JSON file: score the run and add the result to report.md")
    a = ap.parse_args(argv)
    if a.model == "replay" and not a.model_fixtures:
        ap.error("--model replay needs --model-fixtures")
    model = ReplayClient(a.model_fixtures) if a.model == "replay" else None
    try:
        res = run(a.pdf, a.out, a.template, a.force, not a.skip_render, model=model, gold=a.gold)
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
