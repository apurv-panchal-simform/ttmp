"""Phase 1 entry point: intake -> layers -> elements -> zones -> furniture -> router.

    python -m tmp_pipeline.run <pdf> --out out/ [--template file.toml] [--force] [--skip-render]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import logging_ as lg
from .config import load_config
from .furniture import apply_furniture
from .intake import IntakeResult, doc_dir, intake, mark_done
from .layers.reader import read_layers
from .layers.render import Renderer
from .layers.to_elements import build_elements
from .models import ElementStore
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


def run(pdf_path: str | Path, out_root: str | Path, template: str | Path | None = None,
        force: bool = False, render: bool = True) -> RunResult:
    out_root = Path(out_root)
    # intake needs a doc_id before the per-document directory (and its log) exist
    lg.start_run(out_root / "_runs.log")
    try:
        info = intake(pdf_path, out_root)
        out = doc_dir(out_root, info.doc_id)
        if info.already_processed and not force:
            lg.log_event("run", "skipped", reason="already processed", doc_dir=str(out))
            return RunResult(info, out, True)
        out.mkdir(parents=True, exist_ok=True)
    finally:
        lg.end_run()
    run_id = lg.start_run(out / "run.log")
    try:
        lg.set_doc_id(info.doc_id)
        lg.log_event("run", "start", file=Path(pdf_path).name, versions=version_stamp(), scanned=info.scanned,
                     version=info.version)
        cfg = load_config(template)
        layers = read_layers(pdf_path)
        lg.log_event("layers", "read", pages=len(layers), chars=sum(len(p.chars) for p in layers))
        store = build_elements(layers, info.doc_id, cfg)
        assign_zones(store, cfg)
        apply_furniture(store, cfg)
        store.ensure_pixel_boxes()
        store.metadata.update(file=Path(pdf_path).name, version=info.version, scanned=info.scanned,
                              pages=info.pages, run_id=run_id, versions=version_stamp())
        result = route(store, cfg, out / "router.log")
        if render:
            with Renderer(pdf_path) as r:
                for page in range(1, info.pages + 1):
                    path, size = r.save_master(page, out / "master")
                    store.pages[page]["master"] = {"file": f"master/{path.name}", "px": list(size), "dpi": 200}
        store.dump(out / "elements.json")
        (out / "report.md").write_text(build_report(store, result, info), encoding="utf-8")
        mark_done(out_root, info.doc_id)
        lg.log_event("run", "done", elements=len(store.elements), coverage=result.coverage(store)["coverage"])
        return RunResult(info, out, False, store, result)
    finally:
        lg.end_run()


def build_report(store: ElementStore, result: RouterResult, info: IntakeResult) -> str:
    cov = result.coverage(store)
    L = [f"# Phase 1 report: {info.path.name}", "",
         f"- doc_id: `{info.doc_id}`", f"- pages: {info.pages}, version: {info.version}, scanned: {info.scanned}",
         f"- TMP reference: {store.metadata.get('tmp_reference')} (variants seen in page furniture: "
         f"{store.metadata.get('tmp_reference_variants')})",
         f"- elements: {len(store.elements)}, router coverage: {cov['coverage']:.0%} "
         f"(missing {len(cov['missing'])}, duplicated {len(cov['duplicated'])})", ""]
    L += ["## Pages", "", "| Page | Printed | Zone | Reason | Form | Text quality | Elements by type |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for p, i in sorted(store.pages.items()):
        types = Counter(e.type for e in store.on_page(p))
        zones = Counter(e.zone for e in store.on_page(p))
        zone = i["zone"] if len(zones) == 1 or not (set(zones) - {i["zone"]}) else f"{i['zone']} + " + ", ".join(sorted(set(zones) - {i["zone"]}))
        L.append(f"| {p} | {i.get('printed_page', '')} | {zone} | {i['zone_reason']} | {i.get('form_name') or ''} | "
                 f"{i['text_quality']} | " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())) + " |")
    L += ["", "## Routes", "", "| Route | Elements | Outcomes |", "| --- | --- | --- |"]
    by_route: dict[str, Counter] = {}
    for r in result.records:
        by_route.setdefault(r["route"], Counter())[r["outcome"]] += 1
    for k, c in sorted(by_route.items()):
        L.append(f"| {k} | {sum(c.values())} | " + ", ".join(f"{o} {n}" for o, n in sorted(c.items())) + " |")
    flags = Counter(f for e in store.elements.values() for f in e.flags)
    L += ["", "## Flags", ""] + [f"- {k}: {v}" for k, v in sorted(flags.items())]
    low = Counter(e.page for e in store.elements.values() if "low_text_quality" in e.flags)
    L += ["", "## Low text quality (broken font encoding)", "", ", ".join(f"p{p}: {n}" for p, n in sorted(low.items())) or "none"]
    left = [e for e in store.elements.values() if "leftover" in e.flags]
    L += ["", f"## LEFTOVER ({len(left)})", ""]
    L += [f"- {e.element_id} p{e.page} {e.type} box {[round(v) for v in e.bbox_pt]}" for e in left] or ["none"]
    figs = [(e, result.decisions[e.element_id]) for e in store.of_type("image")]
    L += ["", "## Figures", "", "| Element | Page | Area | Route | Triage | Rule |", "| --- | --- | --- | --- | --- | --- |"]
    L += [f"| {e.element_id} | {e.page} | {e.metadata['area_share']:.1%} | {d.route} | {d.triage} | {d.rule} |" for e, d in figs]
    cbs = store.of_type("checkbox")
    L += ["", "## Checkboxes", "", f"- found: {len(cbs)} (ticked {sum(c.metadata['state'] == 'ticked' for c in cbs)}); "
          f"by page: " + ", ".join(f"p{p}: {n}" for p, n in sorted(Counter(c.page for c in cbs).items()))]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp_pipeline.run")
    ap.add_argument("pdf")
    ap.add_argument("--out", default="out")
    ap.add_argument("--template", default=None)
    ap.add_argument("--force", action="store_true", help="re-run even if this file was already processed")
    ap.add_argument("--skip-render", action="store_true")
    a = ap.parse_args(argv)
    res = run(a.pdf, a.out, a.template, a.force, not a.skip_render)
    if res.skipped:
        print(f"already processed ({res.intake.doc_id[:19]}…); use --force to re-run: {res.out_dir}")
        return 0
    cov = res.router.coverage(res.store)
    print(f"{len(res.store.elements)} elements, coverage {cov['coverage']:.0%} -> {res.out_dir}")
    return 0 if cov["coverage"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
