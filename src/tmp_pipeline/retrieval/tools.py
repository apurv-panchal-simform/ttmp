"""The code tools the answer model may call after the chunks are retrieved (plan 5.6, 5.8).

``get_facts``, ``calculate``, ``time_check``, ``find_tmps``, ``compare_versions`` and ``search_chunks``. They are plain code
over the facts store and the search: read-only, run as the signed-in user, and every value goes out with its status,
unit and a citation (form, section, page), so the answer can be checked against exactly what was given. ``calculate``
takes names of stored values only, never a number of its own. A bad argument gives ``{"error": ...}``; a tool never raises.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..calc import calculate as _calculate
from ..calc import named_values
from ..config import Config
from ..ingestion.chunking.model import ChunkSet
from ..ingestion.facts.common import norm_name
from ..ingestion.facts.model import Fact, Facts
from ..records import Status
from .search import ChunkSearch, SearchRequest
from .session import find_tmp, tmp_key
from .store import DocInfo, FactsStore, User, latest
from .timecheck import Calendar, time_check

_PAGE_TAIL = re.compile(r"\s+—\s+pages?\s+[\d, –-]+$")
LABEL_FIELDS = ("road_name", "day_type", "tmd_id", "title", "name", "category", "question", "heading", "speed_kmh")
MAX_ITEMS = 40


@dataclass
class ToolEnv:
    store: FactsStore
    search: ChunkSearch
    cfg: Config
    user: User
    doc: DocInfo | None                      # the TMP the conversation is about (None: no TMP chosen yet)
    now: dt.datetime
    calendar: Calendar | None = None
    seen: set[str] = field(default_factory=set)      # chunk ids already in the context


def pages_text(pages: list[int]) -> str:
    """"page 3", "pages 3, 10", or "pages 1–24" for a run of three or more pages: the words the chunks use, so a page an
    answer cites is also a number the answer check finds in the context."""
    pages = sorted(set(pages))
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    runs: list[str] = []
    start = prev = pages[0]
    for p in [*pages[1:], None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        runs.append(f"{start}–{prev}" if prev - start >= 2 else ", ".join(str(x) for x in range(start, prev + 1)))
        if p is not None:
            start = prev = p
    return "pages " + ", ".join(runs)


class Citer:
    """(form, section, page) for a fact, from the breadcrumb of the chunk that states it (the words the reader sees in
    the context); a fact no chunk states falls back to its pages."""

    def __init__(self, chunks: ChunkSet | None):
        self.crumb: dict[str, str] = {}
        for c in chunks.chunks if chunks else []:
            parts = [p.strip() for p in _PAGE_TAIL.sub("", c.header.split("\n", 1)[0]).split("›")][1:]
            for path in c.field_paths:
                self.crumb.setdefault(path, ", ".join(parts))
                self.crumb.setdefault(path.split(".", 1)[0], ", ".join(parts))

    def __call__(self, fact: Fact | None, pages: list[int] | None = None) -> str:
        crumb = self.crumb.get(fact.fact_id, "") if fact else ""
        where = pages_text(pages if pages is not None else (fact.pages() if fact else []))
        return ", ".join(p for p in (crumb, where) if p)


def _tmp(env: ToolEnv, tmp: str | None) -> tuple[DocInfo | None, dict | None]:
    """The TMP a tool reads: the named one (a TMP key, reference or address the user may read) or the current one."""
    if not tmp:
        if env.doc is None:
            return None, {"error": "no TMP is chosen: pass tmp (a TMP key such as 'KTS 26-1581 V1')"}
        return env.doc, None
    res = find_tmp(env.store, env.user, tmp)
    if res.status == "one" and res.doc is not None:
        return res.doc, None
    names = [tmp_key(d) for d in latest(env.store.docs(env.user))]
    return None, {"error": f"no single readable TMP matches {tmp!r}", "readable": names}


def _facts(env: ToolEnv, doc: DocInfo) -> tuple[Facts, Citer]:
    return env.store.facts(doc.doc_id, env.user), Citer(env.store.chunks(doc.doc_id, env.user))


def _item_label(f: Fact) -> str:
    bits = [str(f.fields[n].value) for n in LABEL_FIELDS if n in f.fields and f.fields[n].value not in (None, "")]
    return f"{f.entity}[{', '.join(bits[:2])}]" if bits else f.fact_id


def _matches(f: Fact, flt: dict[str, Any], cfg: Config) -> bool:
    for name, want in flt.items():
        fv = f.fields.get(name)
        if fv is None:
            return False
        have = fv.value
        if isinstance(want, str) and isinstance(have, str):
            if norm_name(want, cfg.facts) not in norm_name(have, cfg.facts):
                return False
        elif isinstance(have, list):
            if want not in have:
                return False
        elif have != want:
            return False
    return True


def _selected(env: ToolEnv, facts: Facts, entity: str, flt: dict | None) -> list[Fact]:
    return [f for f in facts.of(entity) if f.applicability == "active" and (not flt or _matches(f, flt, env.cfg))]


def entity_catalog(env: ToolEnv) -> str:
    """The stored facts of the current TMP the tools can read, "entity: field, field ...", so the model names an entity and
    fields that exist (the first thing it gets wrong otherwise). Long free-text entities are left out."""
    if env.doc is None:
        return ""
    facts = env.store.facts(env.doc.doc_id, env.user)
    fields: dict[str, dict[str, None]] = {}
    for f in facts.facts:
        if f.applicability == "active" and f.entity not in ("section", "risk_matrix_cell", "figure", "authorisation"):
            fields.setdefault(f.entity, {}).update({n: None for n in f.fields})
    return "\n".join(f"- {e}: {', '.join(list(names)[:14])}" for e, names in fields.items())


# --- get_facts ---------------------------------------------------------------------------------------------------

def get_facts(env: ToolEnv, entity: str, fields: list[str] | None = None, filter: dict | None = None,
              tmp: str | None = None) -> dict:
    doc, err = _tmp(env, tmp)
    if doc is None:
        return err or {}
    facts, cite = _facts(env, doc)
    items = []
    for f in _selected(env, facts, entity, filter)[:MAX_ITEMS]:
        values = {}
        for n, fv in f.fields.items():
            if fields and n not in fields:
                continue
            v: dict[str, Any] = {"value": fv.value}
            if fv.unit:
                v["unit"] = fv.unit
            if fv.raw and str(fv.raw) != str(fv.value):
                v["written"] = fv.raw
            if fv.status != Status.VERIFIED:
                v["status"] = str(fv.status)
            if fv.versions:
                v["versions"] = fv.versions
            values[n] = v
        items.append({"fact": _item_label(f), "values": values, "source": cite(f)})
    if not items:
        known = sorted({f.entity for f in facts.facts})
        return {"tmp": tmp_key(doc), "entity": entity, "items": [], "note": f"no {entity!r} facts; entities: {known}"}
    return {"tmp": tmp_key(doc), "entity": entity, "items": items}


# --- calculate ---------------------------------------------------------------------------------------------------

def calculate(env: ToolEnv, expression: str, entity: str | None = None, filter: dict | None = None,
              values: dict | None = None, result_unit: str | None = None, tmp: str | None = None) -> dict:
    """With ``entity``: once for every fact of that entity (matching ``filter``), naming its own fields, e.g.
    ``rp_to - rp_from``. With ``values``: {name: {"fact": "speed_limit:0.rp_to", "tmp": optional}} from any facts or TMPs."""
    if entity:
        doc, err = _tmp(env, tmp)
        if doc is None:
            return err or {}
        facts, cite = _facts(env, doc)
        results = []
        for f in _selected(env, facts, entity, filter)[:10]:
            res = _calculate(expression, named_values(f.fields, cite(f)), result_unit)
            results.append({"fact": _item_label(f), **res})
        if not results:
            return {"error": f"no {entity!r} facts match", "expression": expression, "entities": entity_catalog(env)}
        return {"tmp": tmp_key(doc), "results": results}
    named: dict[str, dict] = {}
    for name, ref in (values or {}).items():
        path, _, field_name = str((ref or {}).get("fact", "")).partition(".")
        doc, err = _tmp(env, (ref or {}).get("tmp") or tmp)
        if doc is None:
            return err or {}
        facts, cite = _facts(env, doc)
        f = facts.fact(path)
        pick = named_values(f.fields, cite(f)).get(field_name) if f else None
        if pick is None:
            return {"error": f"{name}: {ref!r} is not a stored number or date", "expression": expression}
        named[name] = pick
    if not named:
        return {"error": "give entity (and an expression over its field names) or values", "expression": expression}
    return _calculate(expression, named, result_unit)


# --- time_check --------------------------------------------------------------------------------------------------

def check_time(env: ToolEnv, what: str = "work", date: str | None = None, time: str | None = None,
               day_type: str | None = None, tmp: str | None = None) -> dict:
    doc, err = _tmp(env, tmp)
    if doc is None:
        return err or {}
    facts, cite = _facts(env, doc)
    return {"tmp": tmp_key(doc), **time_check(facts, env.cfg, cite, date, time, day_type, what, env.calendar)}


# --- find_tmps ---------------------------------------------------------------------------------------------------

def _overlaps(info: DocInfo, start: str | None, end: str | None) -> bool:
    if not (start or end):
        return True
    if not (info.work_start and info.work_end):
        return False
    try:
        a, b = dt.date.fromisoformat(info.work_start), dt.date.fromisoformat(info.work_end)
        s = dt.date.fromisoformat(start) if start else a
        e = dt.date.fromisoformat(end) if end else b
    except ValueError:
        return False
    return a <= e and s <= b


def find_tmps(env: ToolEnv, road: str | None = None, suburb: str | None = None, organisation: str | None = None,
              active_from: str | None = None, active_to: str | None = None, approval_status: str | None = None,
              has_conflict: bool | None = None) -> dict:
    """TMPs (latest version of each) the user may read that match every filter given."""
    def has(names: list[str], want: str | None) -> bool:
        return not want or any(norm_name(want, env.cfg.facts) in norm_name(n, env.cfg.facts) for n in names)
    out = []
    for d in latest(env.store.docs(env.user)):
        if not has(d.roads + ([d.site_address] if d.site_address else []), road):
            continue
        if not has(d.suburbs + ([d.suburb] if d.suburb else []), suburb) or not has(d.organisations, organisation):
            continue
        if approval_status and d.approval_status != approval_status:
            continue
        if has_conflict is not None and d.has_conflict != has_conflict:
            continue
        if _overlaps(d, active_from, active_to):
            out.append(d)
    out.sort(key=lambda d: (d.work_start or "", d.tmp_ref))
    return {"count": len(out), "tmps": [
        {"tmp": tmp_key(d), "address": d.site_address or ", ".join(d.roads), "suburb": d.suburb,
         "work_start": d.work_start, "work_end": d.work_end, "approval": d.approval_status,
         "has_conflict": d.has_conflict} for d in out[:MAX_ITEMS]]}


# --- compare_versions --------------------------------------------------------------------------------------------

def _flat(f: Facts) -> dict[str, Any]:
    return {f"{x.fact_id}.{n}": fv.value for x in f.facts for n, fv in x.fields.items()}


def compare_versions(env: ToolEnv, tmp: str | None = None, from_version: int | None = None,
                     to_version: int | None = None) -> dict:
    """Changed fields and sections between two versions of one TMP (default: the two newest)."""
    doc, err = _tmp(env, tmp)
    if doc is None:
        return err or {}
    docs = sorted((d for d in env.store.docs(env.user) if d.tmp_ref == doc.tmp_ref), key=lambda d: (d.version, d.written))
    by_version = {d.version: d for d in docs}
    if from_version is not None and to_version is not None:
        a, b = by_version.get(int(from_version)), by_version.get(int(to_version))
    else:
        a, b = (docs[-2], docs[-1]) if len(docs) >= 2 else (None, None)
    if a is None or b is None:
        return {"error": f"{doc.tmp_ref} has only versions {sorted(by_version)}; two are needed to compare"}
    fa, fb = env.store.facts(a.doc_id, env.user), env.store.facts(b.doc_id, env.user)
    va, vb = _flat(fa), _flat(fb)
    changed = [{"path": p, "from": va.get(p), "to": vb.get(p)} for p in sorted(set(va) | set(vb))
               if va.get(p) != vb.get(p) and not p.startswith("section:")]
    heads = [{str(s.fields["heading"].value) for s in x.of("section") if "heading" in s.fields} for x in (fa, fb)]
    return {"tmp": doc.tmp_ref, "from": f"V{a.version}", "to": f"V{b.version}", "changed": changed[:MAX_ITEMS],
            "sections_added": sorted(heads[1] - heads[0]), "sections_removed": sorted(heads[0] - heads[1]),
            "approval": [fa.approval.status if fa.approval else None, fb.approval.status if fb.approval else None]}


# --- search_chunks -----------------------------------------------------------------------------------------------

def search_chunks(env: ToolEnv, query: str, tmp: str | None = None) -> dict:
    """The best chunks of a TMP for another query (or of another TMP); chunks already in the context are left out."""
    doc, err = _tmp(env, tmp)
    if doc is None:
        return err or {}
    hits = env.search.search(SearchRequest(doc.doc_id, query, env.cfg.retrieval.search_k, include_not_applicable=True),
                             env.user)
    own = doc.doc_id == (env.doc.doc_id if env.doc else "")
    out = [{"chunk": h.chunk.header.split("\n", 1)[0], "content": h.chunk.content} for h in hits
           if not (own and h.chunk.chunk_id in env.seen)]
    env.seen.update(h.chunk.chunk_id for h in hits)
    return {"tmp": tmp_key(doc), "query": query, "chunks": out}


# --- the model's view of the tools ------------------------------------------------------------------------------

def _spec(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required or []}}}


_TMP = {"type": "string", "description": "A TMP key such as 'KTS 26-1581 V1' (default: the current TMP)"}
TOOLS: list[dict] = [
    _spec("get_facts", "Typed values (with unit, status, page) of one entity of a TMP: road, shift_window, speed_limit, "
          "work_period, stage, risk, contact, tmp_document, tmd_sheet ...",
          {"entity": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}},
           "filter": {"type": "object", "description": "field: value pairs the facts must match"}, "tmp": _TMP}, ["entity"]),
    _spec("calculate", "Arithmetic on stored values, done by code, with the working. Never calculate yourself. Give "
          "entity and an expression over that entity's field names (e.g. 'rp_to - rp_from', 'end_date - start_date'), "
          "or values {name: {fact: 'speed_limit:0.rp_to', tmp?}} to combine values of several facts or TMPs. Numbers "
          "may not be written into the expression. + - * / min max abs round and one comparison are allowed.",
          {"expression": {"type": "string"}, "entity": {"type": "string"}, "filter": {"type": "object"},
           "values": {"type": "object"}, "result_unit": {"type": "string", "description": "m, km, min, h, days"},
           "tmp": _TMP}, ["expression"]),
    _spec("time_check", "Whether work, the temporary speed limit or each stage is allowed / in force at a NZ date and "
          "time, from the shift windows, speed-limit hours, works dates and the school-term and public-holiday "
          "calendar. Give a date (YYYY-MM-DD) or a day_type, and a time (HH:MM).",
          {"what": {"type": "string", "enum": ["work", "tsl", "stage"]}, "date": {"type": "string"},
           "time": {"type": "string"},
           "day_type": {"type": "string", "enum": ["school_day", "school_holiday", "weekend", "public_holiday"]},
           "tmp": _TMP}, ["what"]),
    _spec("find_tmps", "TMPs (latest version of each) the user may read, filtered by road, suburb, organisation, dates "
          "the works are active between (YYYY-MM-DD), approval status or whether the document contradicts itself.",
          {"road": {"type": "string"}, "suburb": {"type": "string"}, "organisation": {"type": "string"},
           "active_from": {"type": "string"}, "active_to": {"type": "string"},
           "approval_status": {"type": "string", "enum": ["signed", "partially_signed", "unsigned"]},
           "has_conflict": {"type": "boolean"}}),
    _spec("compare_versions", "What changed between two versions of one TMP (default: the two newest).",
          {"tmp": _TMP, "from_version": {"type": "integer"}, "to_version": {"type": "integer"}}),
    _spec("search_chunks", "Search the chunks of a TMP with a better query, or of another TMP, when what is in the "
          "context is not enough.", {"query": {"type": "string"}, "tmp": _TMP}, ["query"]),
]
_RUN = {"get_facts": get_facts, "calculate": calculate, "time_check": check_time, "find_tmps": find_tmps,
        "compare_versions": compare_versions, "search_chunks": search_chunks}


def run_tool(name: str, args: dict, env: ToolEnv) -> dict:
    fn = _RUN.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}", "tools": sorted(_RUN)}
    try:
        return fn(env, **args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except (ValueError, KeyError, AttributeError) as e:
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}


def render(name: str, args: dict, result: dict) -> str:
    """One call as the context shows it: the tool, its arguments and the result, as compact JSON."""
    return f"{name}({json.dumps(args, ensure_ascii=False)})\n{json.dumps(result, ensure_ascii=False, default=str)}"
