"""Command lines for Phase 4 on a finished run folder (``out/<doc_id>/``), without reading the PDF again.

    tmp-pipeline-facts <doc_dir> [--template file.toml] [--model off|replay --model-fixtures DIR]
        rebuild facts.json and review.json from elements.json and records.json (after a catalog, config or
        check change; plan 6.2: "field mapper, from saved handler results")

    tmp-pipeline-review apply <doc_dir> <corrections.json>
        apply reviewer corrections: values become "reviewed", the original is kept, the revision is bumped
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import logging_ as lg
from ..catalog import CatalogError, load_catalog
from ..config import ConfigError, load_config
from ..errors import PipelineError
from ..model.client import ReplayClient
from ..models import ElementStore, SchemaError, atomic_write_text
from ..records import RecordStore
from .model import Facts, review_json
from .review import apply_corrections_file
from .stage import map_fields, run_checks


def rebuild(doc_dir: str | Path, template: str | Path | None = None, model=None) -> Facts:
    d = Path(doc_dir)
    cfg = load_config(template)
    catalog = load_catalog()
    store = ElementStore.load(d / "elements.json")
    records = RecordStore.load(d / "records.json")
    lg.start_run(d / "facts.log")
    try:
        lg.set_doc_id(store.doc_id)
        with lg.stage("map_fields"):
            facts = map_fields(store, records, cfg, catalog, model)
        with lg.stage("run_checks"):
            run_checks(facts, store, records, cfg, catalog)
        with lg.stage("write_facts"):
            facts.dump(d / "facts.json")
            atomic_write_text(d / "review.json", review_json(facts))
        return facts
    finally:
        lg.end_run()


def main_facts(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-facts", description="Rebuild facts.json from saved records")
    ap.add_argument("doc_dir")
    ap.add_argument("--template", default=None)
    ap.add_argument("--model", choices=("off", "replay"), default="off")
    ap.add_argument("--model-fixtures", default=None)
    a = ap.parse_args(argv)
    if a.model == "replay" and not a.model_fixtures:
        ap.error("--model replay needs --model-fixtures")
    try:
        facts = rebuild(a.doc_dir, a.template, ReplayClient(a.model_fixtures) if a.model == "replay" else None)
    except (PipelineError, ConfigError, CatalogError, SchemaError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{len(facts.facts)} facts, {len(facts.conflicts)} conflicts, approval "
          f"{facts.approval.status if facts.approval else 'unknown'}, {len(facts.review)} review items")
    return 0


def main_review(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-review", description="Apply reviewer corrections to facts.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply")
    p.add_argument("doc_dir")
    p.add_argument("corrections")
    a = ap.parse_args(argv)
    try:
        touched = apply_corrections_file(a.doc_dir, a.corrections)
    except (PipelineError, SchemaError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"applied; {len(touched)} value(s) reviewed: {', '.join(touched)}")
    return 0
