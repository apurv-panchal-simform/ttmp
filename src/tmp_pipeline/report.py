"""report.md: what a run found, in one page a person can check against the PDF."""
from __future__ import annotations

from collections import Counter

from .intake import IntakeResult
from .models import ElementStore
from .records import RecordStore
from .router import RouterResult

UNVERIFIED_LIST_LIMIT = 40


def _phase1(store: ElementStore, result: RouterResult, info: IntakeResult) -> list[str]:
    cov = result.coverage(store)
    L = [f"# Run report: {info.path.name}", "",
         f"- doc_id: `{info.doc_id}`", f"- pages: {info.pages}, version: {info.version}, scanned: {info.scanned}",
         f"- TMP reference: {store.metadata.get('tmp_reference')} (variants seen in page furniture: "
         f"{store.metadata.get('tmp_reference_variants')})",
         f"- elements: {len(store.elements)}, router coverage: {cov['coverage']:.0%} "
         f"(missing {len(cov['missing'])}, duplicated {len(cov['duplicated'])})", ""]
    L += ["## Pages", "", "| Page | Printed | Zone | Reason | Form | Text quality | Elements by type |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for p, i in sorted(store.pages.items()):
        types = Counter(e.type for e in store.on_page(p))
        zones = Counter(e.zone for e in store.on_page(p))
        extra = set(zones) - {i.zone}
        zone = i.zone if len(zones) == 1 or not extra else f"{i.zone} + " + ", ".join(sorted(extra))
        L.append(f"| {p} | {i.printed_page or ''} | {zone} | {i.zone_reason} | {i.form_name or ''} | "
                 f"{i.text_quality} | " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())) + " |")
    L += ["", "## Routes", "", "| Route | Elements | Outcomes |", "| --- | --- | --- |"]
    by_route: dict[str, Counter] = {}
    for r in result.records:
        by_route.setdefault(r["route"], Counter())[r["outcome"]] += 1
    for k, c in sorted(by_route.items()):
        L.append(f"| {k} | {sum(c.values())} | " + ", ".join(f"{o} {n}" for o, n in sorted(c.items())) + " |")
    flags = Counter(f for e in store.elements.values() for f in e.flags)
    L += ["", f"## Handler failures ({len(result.failures)})", ""]
    L += [f"- {f['element'] or 'finish'} ({f['route']}): {f['error']}" for f in result.failures] or ["none"]
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
    return L




def _records(records: RecordStore) -> list[str]:
    L = ["", "## Records", "", "| Kind | Records | With unverified values | Not applicable |", "| --- | --- | --- | --- |"]
    by_kind: dict[str, list] = {}
    for r in records.records.values():
        by_kind.setdefault(r.kind, []).append(r)
    for kind, recs in sorted(by_kind.items()):
        unv = sum(any(f.status == "unverified" for f in r.fields.values()) or r.status == "unverified" for r in recs)
        L.append(f"| {kind} | {len(recs)} | {unv} | {sum(r.applicability == 'not_applicable' for r in recs)} |")
    unverified = [(r, n, f) for r in records.records.values() for n, f in r.fields.items() if f.status == "unverified"]
    L += ["", f"### Unverified values ({len(unverified)})", ""]
    L += [f"- {r.record_id} `{n}` = {f.value!r} (raw {f.raw!r}): {'; '.join(f.notes) or 'no reason recorded'}"
          for r, n, f in unverified[:UNVERIFIED_LIST_LIMIT]] or ["none"]
    if len(unverified) > UNVERIFIED_LIST_LIMIT:
        L.append(f"- ... and {len(unverified) - UNVERIFIED_LIST_LIMIT} more (see records.json)")
    mism = [r for r in records.records.values() if "rating_mismatch" in r.flags]
    L += ["", f"### Rating mismatches ({len(mism)})", "", "Stated rating differs from the matrix cell at (likelihood, impact); "
          "nothing was corrected.", ""]
    for r in mism:
        for stage, checks in r.data.get("rating_checks", {}).items():
            for c in checks:
                if c["matrix"] and c["stated"] != c["matrix"]:
                    cat = r.fields["category"].raw if "category" in r.fields else r.record_id
                    L.append(f"- p{r.page} {cat} ({stage}): stated {c['stated']}, matrix (L{c['likelihood']} x I{c['impact']}, "
                             f"score {c['score']}) says {c['matrix']}")
    refs = [r for r in records.records.values() if "refers_elsewhere" in r.flags]
    L += ["", f"### Values that refer elsewhere ({len(refs)})", ""]
    L += [f"- {r.record_id} ({r.kind}, p{r.page}): " + ", ".join(
        f"{n} = {f.raw!r}" for n, f in r.fields.items() if f.notes and "refers" in f.notes[0]) for r in refs] or ["none"]
    struck = [r for r in records.records.values() if r.applicability == "not_applicable"]
    L += ["", f"### Struck-through (not applicable) records ({len(struck)})", ""]
    L += [f"- {r.record_id} ({r.kind}, p{r.page}): {r.data.get('heading') or ''}" for r in struck] or ["none"]
    boxes = [r for r in records.records.values() if r.kind in ("checkbox", "yes_no")]
    L += ["", "### Checkbox answers (confirm against the PDF)", "", "| Page | Label or question | Layout | Answer | Note |",
          "| --- | --- | --- | --- | --- |"]
    L += [f"| {r.page} | {(r.data.get('label') or r.data.get('question') or '')[:70]} | {r.data.get('layout')} | "
          f"{r.data.get('answer')} | {(r.data.get('note') or '')[:40]} |" for r in boxes]
    return L


def build_report(store: ElementStore, result: RouterResult, info: IntakeResult, records: RecordStore | None = None,
                 gold_text: str | None = None) -> str:
    L = _phase1(store, result, info)
    if records is not None:
        L += _records(records)
    if gold_text:
        L += ["", "## Gold set", "", gold_text.rstrip()]
    return "\n".join(L) + "\n"
