"""report.md: what a run found, in one page a person can check against the PDF."""
from __future__ import annotations

from collections import Counter

from .chunks.model import ChunkSet
from .facts.model import Facts
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


def _diagrams(store: ElementStore, records: RecordStore) -> list[str]:
    sheets = records.of_kind("diagram_sheet")
    figures = records.of_kind("figure")
    if not sheets and not figures:
        return []
    L = ["", "## Diagrams and images", ""]
    if sheets:
        L += ["| Page | Sheet | Title | Rev | Vector | Symbols | Needs caption | Low-quality lines | Summary | Status |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in sorted(sheets, key=lambda x: x.page):
            d = r.data
            syms = d["symbols"]
            summary = "none (no model)" if d.get("summary") is None else ("verified" if d["summary"]["verified"] else "unverified claims")
            L.append(f"| {r.page} | {d['sheet']} | {d['title']} | {d['rev']} | {'yes' if d['vector'] else 'NO (fallback)'} | {len(syms)} | "
                     f"{sum(bool(s['needs_caption']) for s in syms)} | {len(d['low_quality_lines'])} | {summary} | {r.status} |")
        L += ["", "### Symbols by class", "", "| Page | Classes |", "| --- | --- |"]
        for r in sorted(sheets, key=lambda x: x.page):
            by = Counter(str(s["kind"]) for s in r.data["symbols"])
            L.append(f"| {r.page} | " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())) + " |")
        reused = [(r.page, i) for r in sheets for i in r.data["images"]["placed"] if i["reused_on"] and i["role"] == "aerial"]
        L += ["", "### Image lineage", ""]
        L += [f"- p{pg}: the aerial photo is also used on p{', p'.join(map(str, i['reused_on']))}" for pg, i in reused] or ["no reused aerials"]
        pend = [(r.page, s) for r in sheets for s in r.data["symbols"] if s["needs_caption"] and not s["extra"].get("caption")]
        L += ["", f"### Symbols waiting for a caption ({len(pend)})", "",
              "Pictograms with no text; X10 names them when a model is available." if pend else "none"]
    if figures:
        L += ["", "### Figures read", "", "| Element | Page | Status | Labels | Numbers (verified/total) | Reused from |",
              "| --- | --- | --- | --- | --- | --- |"]
        for r in figures:
            nums = r.data.get("numbers", [])
            L.append(f"| {r.evidence[0].element_id} | {r.page} | {r.status} | {len(r.data.get('labels', []))} | "
                     f"{sum(n['status'] == 'verified' for n in nums)}/{len(nums)} | {r.data.get('reused_from') or ''} |")
    return L


def _cell(v: object, width: int = 70) -> str:
    text = " ".join(str(v).split()).replace("|", "/")
    return text if len(text) <= width else text[:width - 1] + "…"


def _facts(facts: Facts) -> list[str]:
    L = ["", "## Facts and checks", "",
         f"{len(facts.facts)} facts from {sum(len(f.sources) for f in facts.facts)} sources; "
         f"{len(facts.other)} records hold no fact (tables, rows, matrix keys). Revision {facts.revision}."]
    if facts.approval is not None:
        a = facts.approval
        L += ["", f"### Approval: {a.status}", "", "| Role | Page | Status | Name | Signature | Date |",
              "| --- | --- | --- | --- | --- | --- |"]
        L += [f"| {s.role} | {s.page} | {s.status} | {_cell(s.name or '')} | {_cell(s.signature or '')} | {s.date or ''} |"
              for s in facts.signoffs]
        L += [f"- {n}" for n in a.notes]
    L += ["", "### Conflicts", ""]
    if facts.conflicts:
        L += ["| Id | Rule | Conflict | Versions (pages) |", "| --- | --- | --- | --- |"]
        for c in facts.conflicts:
            versions = "; ".join(f"{_cell(v.get('value'), 50)} (p. {', '.join(map(str, v.get('pages', [])))})"
                                 for v in c.versions)
            L.append(f"| {c.conflict_id} | {c.rule} | {c.title} | {versions} |")
    else:
        L.append("None.")
    L += ["", "### Checks", "", "| Rule | Check | Outcome | Detail |", "| --- | --- | --- | --- |"]
    L += [f"| {c.rule} | {c.name} | {c.outcome}{' ' + c.conflict_id if c.conflict_id else ''} | {_cell(c.detail, 90)} |"
          for c in facts.checks]
    if facts.tmd_sheets:
        L += ["", "### Diagram register and mentions", "", "| Sheet | Title | Page | Mentions (pages) | Aliases from conflicts |",
              "| --- | --- | --- | --- | --- |"]
        for sh in facts.tmd_sheets:
            pages = sorted({m.page for m in facts.mentions if m.mapped_to == sh.tmd_id})
            extra = [m.text for m in facts.mentions if m.mapped_to == sh.tmd_id and m.id_conflict]
            L.append(f"| {sh.tmd_id} | {sh.title} | {sh.page} | {', '.join(map(str, pages))} | "
                     f"{_cell('; '.join(dict.fromkeys(extra)), 80)} |")
        unmatched = [m for m in facts.mentions if m.mapped_to is None]
        L += [f"- unmatched mention p{m.page}: {m.text!r} ({'; '.join(m.notes)})" for m in unmatched]
    pointers = [(f, k, v) for f in facts.facts for k, v in f.fields.items() if v.method == "pointer" or
                (v.value is None and any("refers elsewhere" in n for n in v.notes))]
    if pointers:
        L += ["", "### Pointers", ""]
        L += [f"- {f.fact_id}.{k} ({_cell(v.raw or '', 40)}): " + (", ".join(v.link) if v.link else "unresolved")
              for f, k, v in pointers]
    merged = [f for f in facts.facts if len({s.record_id for s in f.sources}) > 1]
    if merged:
        L += ["", "### Merged records", ""]
        L += [f"- {f.fact_id} from {', '.join(sorted({s.record_id for s in f.sources if s.record_id}))}" for f in merged]
    if facts.review:
        counts: dict[str, int] = {}
        for r in facts.review:
            counts[r.kind] = counts.get(r.kind, 0) + 1
        L += ["", "### Review list", "", ", ".join(f"{k}: {n}" for k, n in sorted(counts.items())) +
              f" (open: {sum(r.status == 'open' for r in facts.review)}); details in review.json."]
    cov = facts.coverage
    if cov:
        L += ["", f"Coverage: {'complete' if cov.get('complete') else 'INCOMPLETE'}; "
                  f"records unaccounted: {len(cov.get('records_unaccounted', []))}, "
                  f"elements without outcome: {len(cov.get('without_outcome', []))}."]
    return L


def _chunks(chunks: ChunkSet, lineage: dict | None) -> list[str]:
    by_type: dict[str, list[int]] = {}
    for c in chunks.chunks:
        by_type.setdefault(c.chunk_type, []).append(c.tokens)
    L = ["", "## Chunks", "", f"{len(chunks.chunks)} chunks; summaries: {sum(bool(c.summary) for c in chunks.chunks)}; "
         f"vectors: {sum(bool(c.vector_key) for c in chunks.chunks)}.", "",
         "| Type | Chunks | Tokens (min / median / max) |", "| --- | --- | --- |"]
    for t, ns in sorted(by_type.items()):
        ns = sorted(ns)
        L.append(f"| {t} | {len(ns)} | {ns[0]} / {ns[len(ns) // 2]} / {ns[-1]} |")
    if lineage is not None:
        L += ["", f"Lineage: {'complete' if lineage['complete'] else 'GAPS'} ({lineage['fields']} fields, "
                  f"{lineage['sections']} sections, {lineage['images']} images, {lineage['leftovers']} leftovers; "
                  f"{len(lineage['exempt'])} records without text exempt)."]
        L += [f"- gap: {g['kind']} {g['ref']} ({g.get('text', '')})" for g in lineage["gaps"][:40]]
    return L


def build_report(store: ElementStore, result: RouterResult, info: IntakeResult, records: RecordStore | None = None,
                 gold_text: str | None = None, facts: Facts | None = None, chunks: ChunkSet | None = None,
                 lineage: dict | None = None) -> str:
    L = _phase1(store, result, info)
    if records is not None:
        L += _records(records)
        L += _diagrams(store, records)
    if facts is not None:
        L += _facts(facts)
    if chunks is not None:
        L += _chunks(chunks, lineage)
    if gold_text:
        L += ["", "## Gold set", "", gold_text.rstrip()]
    return "\n".join(L) + "\n"
