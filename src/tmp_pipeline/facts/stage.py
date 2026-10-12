"""Phase 4 stages (plan 6.1, steps 13-14): ``map_fields`` builds the canonical facts from handler records,
``run_checks`` runs the conflict rules, approval, coverage and the review list.

Both read only the element store and the records (what ``elements.json`` and ``records.json`` hold), so a catalog
or check change re-runs them from saved results without reading the PDF again (plan 6.2).
"""
from __future__ import annotations

import re

from .. import logging_ as lg
from ..catalog import Catalog
from ..config import Config
from ..model import prompts
from ..model.client import ModelClient
from ..models import ElementStore
from ..records import RecordStore, Status
from ..versions import version_stamp
from .checks import Ctx, coverage, run_rules
from .common import norm_name, source_of
from .links import link_sections, link_tmds
from .mentions import find_mentions, stage_lists
from .merge import copy_records, merge_entities, merge_values
from .model import Fact, Facts, FactValue, Source
from .parse import contacts_from_pairs, order_reference, stage_entries, tmp_references, tsl_rows
from .pointers import resolve_pointers
from .review import build_review
from .signoff import approval, read_signoffs
from .tmd import map_mentions, sheet_register

_ITEM = re.compile(r"(?:^|\s)\d+\.\s+(.+?)(?=\s\d+\.\s|\.\s|\.$|$)")


def _tmp_document(facts: Facts, store: ElementStore, records: RecordStore, cfg: Config) -> Fact:
    occ = tmp_references(store, records)
    fields: dict[str, FactValue] = {}
    if occ:
        values: dict[str, list] = {}
        for o in occ:
            values.setdefault(o.value, []).append(o)
        sources = [o.source for o in occ]
        if len(values) == 1:
            (value, _), = values.items()
            fields["tmp_reference"] = FactValue(value, value, sources=sources, method="pattern")
        else:
            versions = [{"value": v, "pages": sorted({o.source.page for o in os}),
                         "sources": sorted({o.source.record_id or o.method for o in os})}
                        for v, os in sorted(values.items(), key=lambda kv: -len(kv[1]))]
            fields["tmp_reference"] = FactValue(None, None, None, Status.CONFLICT, "check", sources,
                                                ["the document states more than one TMP reference"], versions)
    order = order_reference(records, cfg.facts)
    if order is not None:
        fields["order_reference"] = order
    return facts.add("tmp_document", fields=fields, sources=[s for fv in fields.values() for s in fv.sources])


def _merge_rows(facts: Facts, entity: str, rows: list[tuple[str, dict[str, FactValue], list[Source]]],
                cfg: Config, catalog: Catalog) -> None:
    """Rows already keyed (stage by TMD id, contact by name): one fact per key with merged values."""
    by_key: dict[str, list[tuple[dict[str, FactValue], list[Source]]]] = {}
    for key, fields, sources in rows:
        by_key.setdefault(key, []).append((fields, sources))
    for parts in by_key.values():
        names = list(dict.fromkeys(k for f, _ in parts for k in f))
        ent = catalog.facts_entities.get(entity)
        merged = {}
        for k in names:
            items = [(f[k], f[k].sources[0] if f[k].sources else src[0]) for f, src in parts if k in f]
            if ent is not None and k in ent.fields and ent.fields[k].type == "list":   # lists join, never clash
                joined = list(dict.fromkeys(x for fv, _ in items for x in fv.value))
                merged[k] = FactValue(joined, None, sources=[s for _, s in items], method="merge")
            else:
                merged[k] = merge_values(items, cfg.facts)
        facts.add(entity, fields=merged, sources=[s for _, src in parts for s in src])


def map_fields(store: ElementStore, records: RecordStore, cfg: Config, catalog: Catalog,
               model: ModelClient | None = None) -> Facts:
    fc = cfg.facts
    facts = Facts(records.doc_id or store.doc_id, version_stamp(), cfg.config_hash(), catalog.version)
    used: set[str] = set()

    facts.tmd_sheets = sheet_register(records)
    used |= {s.record_id for s in facts.tmd_sheets}
    facts.mentions = find_mentions(records, fc)
    if map_mentions(facts.mentions, facts.tmd_sheets, fc, catalog, model,
                    {m.mention_id: m.source.raw or m.text for m in facts.mentions}):
        facts.prompt_versions["X11"] = prompts.PROMPT_VERSIONS["X11"]
    lg.log_event("map_fields", "mentions", sheets=len(facts.tmd_sheets), mentions=len(facts.mentions),
                 id_conflicts=sum(m.id_conflict for m in facts.mentions),
                 unmatched=sum(m.mapped_to is None for m in facts.mentions))

    used |= merge_entities(facts, list(records.records.values()), catalog, fc)
    used |= copy_records(facts, list(records.records.values()), catalog)
    _tmp_document(facts, store, records, cfg)

    _merge_rows(facts, "stage", [
        (e.tmd_id, {"tmd_id": FactValue(e.tmd_id, sources=[e.source], method="mention"),
                    "title": FactValue(e.title, e.title, sources=[e.source], method="mention"),
                    **({"hours": FactValue(e.hours, e.hours, sources=[e.source], method="pattern")}
                       if e.hours else {})}, [e.source])
        for e in stage_entries(stage_lists(facts.mentions, fc), records)], cfg, catalog)

    roads = [str(f.fields["road_name"].value) for f in facts.of("road") if "road_name" in f.fields]
    for row in tsl_rows(records, roads, fc):
        tmds = sorted({m.mapped_to for m in facts.mentions if m.source.record_id == row.record_id and m.mapped_to})
        if tmds:
            row.fields["tmd_ids"] = FactValue(tmds, None, sources=[row.source], method="mention")
        facts.add("speed_limit", fields=row.fields, sources=[row.source])

    tables = read_signoffs(store, records, fc)
    facts.signoffs = [s for t in tables for s in t.signoffs]
    used |= {t.record.record_id for t in tables}
    contacts: list[tuple[str, dict[str, FactValue], list[Source]]] = []
    for s in facts.signoffs:
        if s.name:
            vals = {"name": s.name, "organisation": s.organisation, "phone": s.phone, "email": s.email}
            fields = {k: FactValue(v, v, sources=s.sources) for k, v in vals.items() if v}
            fields["roles"] = FactValue([s.role], s.role, sources=s.sources)
            contacts.append((norm_name(s.name, fc), fields, s.sources))
    for item, src in contacts_from_pairs(records, fc):
        fields = {k: FactValue(v, v, sources=[src]) for k, v in item.items() if k != "role"}
        fields["roles"] = FactValue([item["role"]], item["role"], sources=[src])
        contacts.append((norm_name(item["name"], fc), fields, [src]))
    _merge_rows(facts, "contact", contacts, cfg, catalog)

    for t in tables:
        if any(s.role == "rca_authorisation_form3" for s in t.signoffs):
            items = [i.strip() for i in _ITEM.findall(t.statement)]
            answers = [{"question": r.data.get("question"), "answer": r.data.get("answer")}
                       for r in records.of_kind("yes_no", page=t.record.page)]
            src = source_of(t.record, t.statement)
            facts.add("authorisation", fields={
                "items": FactValue(items, t.statement, sources=[src], method="pattern",
                                   status=Status.VERIFIED if items else Status.UNVERIFIED,
                                   notes=[] if items else ["no numbered items in the request"]),
                "answers": FactValue(answers, None, sources=[src], method="checkbox")}, sources=[src])

    unresolved = resolve_pointers(facts, store, fc)
    link_tmds(facts)
    link_sections(facts, store)
    facts.other = [{"record_id": r.record_id, "kind": r.kind, "page": r.page}
                   for r in records.records.values() if r.record_id not in used]
    lg.log_event("map_fields", "facts", facts=len(facts.facts), other=len(facts.other),
                 signoffs=len(facts.signoffs), unresolved_pointers=len(unresolved))
    return facts


def run_checks(facts: Facts, store: ElementStore, records: RecordStore, cfg: Config, catalog: Catalog) -> Facts:
    ctx = Ctx(facts, store, records, cfg.facts)
    run_rules(ctx)
    form3 = [s for s in facts.signoffs if s.role == "rca_authorisation_form3"]
    auth = next(iter(facts.of("authorisation")), None)
    requested = bool(facts.of("speed_limit")) or bool(
        auth and "items" in auth.fields and any(cfg.facts.tsl_requested.search(str(i))
                                                for i in auth.fields["items"].value or []))
    facts.approval = approval(facts.signoffs, catalog, {"tsl_requested": requested})
    if form3 and not requested:
        facts.approval.notes.append("Form 3 is present but no temporary speed limit is requested")
    doc = next(iter(facts.of("tmp_document")), None)
    if doc is not None:
        doc.fields["approval_status"] = FactValue(facts.approval.status, None, method="check",
                                                  sources=[s for so in facts.signoffs for s in so.sources])
    accounted = {o["record_id"] for o in facts.other}
    accounted |= {s.record_id for f in facts.facts for s in f.sources}
    accounted |= {s.record_id for s in facts.tmd_sheets}
    accounted |= {src.record_id for so in facts.signoffs for src in so.sources}
    facts.coverage = coverage(ctx, accounted)
    facts.review = build_review(facts)
    lg.log_event("run_checks", "done", conflicts=len(facts.conflicts), approval=facts.approval.status,
                 review=len(facts.review), coverage_complete=facts.coverage["complete"])
    return facts
