"""Field mapper, merging (plan section 3.2).

Records of one catalog entity are merged by the entity's ``match_key`` after names are normalised ("Ave" ->
"Avenue"). When a key field is missing on one side, records still merge if the keys they both have agree and the
match is unique. An entity marked ``one_per_document`` is always one fact. Values that agree keep every source;
values that clash become a ``conflict`` with every version kept and no value chosen.
"""
from __future__ import annotations

from typing import Any

from ...config import FactsCfg
from ...records import Record, Status
from ..catalog import Catalog, EntityDef
from .common import combine_status, day_type, norm_name, same, source_of
from .model import Facts, FactValue, Source


def _key(record: Record, ent: EntityDef, cfg: FactsCfg) -> dict[str, str]:
    out = {}
    for k in ent.match_key:
        fv = record.fields.get(k)
        if fv is None or fv.value is None or fv.value == "":
            continue
        text = str(fv.value)
        out[k] = (day_type(text, cfg) or norm_name(text, cfg)) if k == "day_type" else norm_name(text, cfg)
    return out


def group_records(records: list[Record], ent: EntityDef, cfg: FactsCfg) -> list[list[Record]]:
    if ent.one_per_document:
        return [records] if records else []
    if not ent.match_key:
        return [[r] for r in records]
    groups: list[tuple[dict[str, str], list[Record]]] = []
    for r in records:
        key = _key(r, ent, cfg)
        fits = [g for g in groups
                if key and set(key) & set(g[0]) and all(g[0][k] == v for k, v in key.items() if k in g[0])]
        if len(fits) == 1:
            fits[0][0].update(key)
            fits[0][1].append(r)
        else:
            groups.append((dict(key), [r]))
    return [g for _, g in groups]


def merge_values(items: list[tuple[FactValue, Source]], cfg: FactsCfg) -> FactValue:
    """One value from every record's value of a field. Missing values never clash with present ones."""
    present = [(fv, s) for fv, s in items if fv.value is not None and fv.value != ""]
    distinct: list[tuple[Any, list[tuple[FactValue, Source]]]] = []
    for fv, s in present:
        for d in distinct:
            if same(d[0], fv.value, cfg):
                d[1].append((fv, s))
                break
        else:
            distinct.append((fv.value, [(fv, s)]))
    sources = [s for _, s in items]
    notes = list(dict.fromkeys(n for fv, _ in items for n in fv.notes))
    if len(distinct) > 1:
        versions = [{"value": v, "raw": grp[0][0].raw, "pages": sorted({s.page for _, s in grp}),
                     "sources": [s.record_id for _, s in grp]} for v, grp in distinct]
        return FactValue(None, None, items[0][0].unit, Status.CONFLICT, "merge", sources,
                         [*notes, "the document gives different values"], versions)
    base = present[0][0] if present else items[0][0]
    method = base.method if len(items) == 1 else "merge"
    return FactValue(base.value, base.raw, base.unit, combine_status([fv for fv, _ in items]), method, sources, notes)


def fact_value(record: Record, fname: str) -> FactValue:
    fv = record.fields[fname]
    return FactValue(fv.value, fv.raw, fv.unit, fv.status, fv.method, [source_of(record, fv.raw)], list(fv.notes))


def merge_entities(facts: Facts, records: list[Record], catalog: Catalog, cfg: FactsCfg) -> set[str]:
    """Add one fact per merged group of every catalog entity. Returns the ids of the records used."""
    used: set[str] = set()
    by_entity: dict[str, list[Record]] = {}
    for r in sorted(records, key=lambda r: (r.page, r.record_id)):
        if r.entity in catalog.entities and (r.kind == r.entity or r.kind == "checkbox"):
            by_entity.setdefault(r.entity, []).append(r)
    for ename in catalog.entities:
        for group in group_records(by_entity.get(ename, []), catalog.entities[ename], cfg):
            fields: dict[str, list[tuple[FactValue, Source]]] = {}
            for r in group:
                for fname in r.fields:
                    fv = fact_value(r, fname)
                    fields.setdefault(fname, []).append((fv, fv.sources[0]))
            facts.add(ename, fields={k: merge_values(v, cfg) for k, v in fields.items()},
                      sources=[source_of(r) for r in group],
                      applicability="not_applicable" if all(r.applicability == "not_applicable" for r in group)
                      else "active",
                      flags=list(dict.fromkeys(str(f) for r in group for f in r.flags)))
            used |= {r.record_id for r in group}
    return used


def copy_records(facts: Facts, records: list[Record], catalog: Catalog) -> set[str]:
    """Records of the kinds in ``record_kinds`` become one fact each (risk-matrix cells, figures, form answers,
    sections). A section keeps its heading and type as fields beside the catalog facts read from it."""
    used: set[str] = set()
    for r in sorted(records, key=lambda r: (r.page, r.record_id)):
        entity = catalog.record_kinds.get(r.kind)
        if entity is None or (r.kind == "checkbox" and r.entity in catalog.entities):
            continue
        fields = {k: fact_value(r, k) for k in r.fields}
        if r.kind == "section":
            src = source_of(r)
            for key in ("heading", "section_type"):
                if r.data.get(key) is not None:
                    fields.setdefault(key, FactValue(r.data[key], str(r.data[key]), sources=[src]))
        if r.kind == "yes_no":
            fields.setdefault("question", FactValue(r.data.get("question"), r.data.get("question"),
                                                    sources=[source_of(r)]))
        facts.add(entity, fields=fields, sources=[source_of(r)], applicability=r.applicability,
                  flags=[str(f) for f in r.flags])
        used.add(r.record_id)
    return used
