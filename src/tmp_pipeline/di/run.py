"""DI track CLI. Works on a finished Phase 1 output directory.

    python -m tmp_pipeline.di.run <pdf> --doc-dir out/sha256-... [--cache-only | --live]

Default (no flag) only evaluates the trigger rules and writes di_triggers.json: no call, no cost.
--cache-only reads a cached DI result and fails if there is none. --live calls the service when the cache misses.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .. import logging_ as lg
from ..models import ElementStore
from .cache import CacheMiss, DiCache
from .client import analyze
from .to_elements import di_to_elements
from .triggers import di_triggers


def run(pdf: str | Path, doc_dir: str | Path, *, cache_only: bool = False, live: bool = False) -> dict:
    doc_dir = Path(doc_dir)
    core = ElementStore.load(doc_dir / "elements.json")
    lg.start_run(doc_dir / "di.log")
    try:
        lg.set_doc_id(core.doc_id)
        triggers = di_triggers(core)
        (doc_dir / "di_triggers.json").write_text(json.dumps([t.as_dict() for t in triggers], indent=1))
        summary = Counter(t.reason for t in triggers)
        lg.log_event("di", "triggers", total=len(triggers), by_reason=dict(summary),
                     pages=sorted({t.page for t in triggers}))
        out = {"triggers": len(triggers), "by_reason": dict(summary), "called": False, "elements": 0}
        if not (cache_only or live):
            return out
        sha = core.doc_id.split(":", 1)[1]
        result = analyze(pdf, sha, DiCache(doc_dir / "di"), cache_only=cache_only)
        regions: dict[int, list] = {}
        for t in triggers:
            if t.bbox_pt is not None:
                regions.setdefault(t.page, []).append(t.bbox_pt)
        whole = {t.page for t in triggers if t.bbox_pt is None}
        store = di_to_elements(result, core, None if whole else regions)
        store.dump(doc_dir / "di_elements.json")
        out.update(called=True, elements=len(store.elements))
        return out
    finally:
        lg.end_run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp_pipeline.di.run")
    ap.add_argument("pdf")
    ap.add_argument("--doc-dir", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cache-only", action="store_true")
    g.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run(a.pdf, a.doc_dir, cache_only=a.cache_only, live=a.live)
    except CacheMiss as e:
        print(f"no cached DI result for key {e}; run with --live to fetch one", file=sys.stderr)
        return 2
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
