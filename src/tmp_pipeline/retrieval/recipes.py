"""Step 3, recipes per question type (plan 5.5): code maps each type to its tools, runs them in parallel and merges
the results. A question with several types runs several recipes; a low-confidence reading runs the baseline
(``doc_card`` + ``search_chunks``). Exact values come from the facts, wording from chunks, sums and times from code.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .. import logging_ as lg
from .. import tracing
from ..catalog import normalise
from ..chunks.model import ChunkSet
from ..config import Config
from ..facts.common import day_type as facts_day_type
from ..facts.common import norm_name
from ..facts.model import Conflict, Facts
from .calc import calculate
from .classify import Reading
from .hybrid import ChunkSearch, Hit, SearchRequest
from .store import DocInfo, FactsStore, User
from .timecheck import Calendar, time_check
from .tools import (
    Citer,
    FactItem,
    compare_versions,
    conflicts_for,
    fact_item,
    find_sheet,
    find_tmps,
    get_facts,
    get_status,
    get_tmd,
)

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Evidence:
    """Everything the tools returned for one question, in the order the context shows it."""
    facts: list[FactItem] = field(default_factory=list)
    calcs: list[dict] = field(default_factory=list)
    times: list[dict] = field(default_factory=list)
    status: dict | None = None
    tmd: list[dict] = field(default_factory=list)
    tmps: list[DocInfo] = field(default_factory=list)
    versions: dict | None = None
    chunks: list[Hit] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)       # conflicts on the values given
    notes: list[str] = field(default_factory=list)                # what could not be checked, and why
    calls: list[dict] = field(default_factory=list)               # tool, arguments, ms, result size
    entities: list[str] = field(default_factory=list)

    def merge(self, other: Evidence) -> None:
        have = {f.fact_id for f in self.facts}
        self.facts += [f for f in other.facts if f.fact_id not in have]
        self.calcs += other.calcs
        self.times += other.times
        self.status = self.status or other.status
        self.tmd += [t for t in other.tmd if t.get("tmd_id") not in {x.get("tmd_id") for x in self.tmd}]
        self.tmps += [d for d in other.tmps if d.doc_id not in {x.doc_id for x in self.tmps}]
        self.versions = self.versions or other.versions
        seen = {h.chunk.chunk_id for h in self.chunks}
        for h in other.chunks:
            if h.chunk.chunk_id not in seen:
                self.chunks.append(h)
                seen.add(h.chunk.chunk_id)
        self.conflicts += [c for c in other.conflicts if c.conflict_id not in {x.conflict_id for x in self.conflicts}]
        self.notes += [n for n in other.notes if n not in self.notes]
        self.calls += other.calls
        self.entities += [e for e in other.entities if e not in self.entities]


@dataclass
class Tools:
    """The tools bound to one TMP and one user (tools run as the user, plan 5.6)."""
    store: FactsStore
    search: ChunkSearch
    cfg: Config
    user: User
    doc: DocInfo | None
    calendar: Calendar | None = None
    _facts: Facts | None = None
    _chunks: ChunkSet | None = None

    @property
    def facts(self) -> Facts:
        if self._facts is None:
            assert self.doc is not None
            self._facts = self.store.facts(self.doc.doc_id, self.user)
        return self._facts

    @property
    def chunks(self) -> ChunkSet | None:
        if self._chunks is None and self.doc is not None:
            self._chunks = self.store.chunks(self.doc.doc_id, self.user)
        return self._chunks

    def call(self, ev: Evidence, name: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        args = {k: v for k, v in kwargs.items()
                if k not in ("facts", "chunks", "cfg", "store", "user", "cite", "calendar")}
        kind = "retriever" if name == "search_chunks" else "tool"
        with tracing.span(f"tool {name}", kind, **{"tmp.tool": name, "tmp.doc_id": self.doc.doc_id if self.doc else None,
                                                   **{f"tmp.arg.{k}": v for k, v in args.items()
                                                      if isinstance(v, str | int | float | bool)}}) as sp:
            sp.input({k: (v.__dict__ if hasattr(v, "__dict__") else v) for k, v in args.items()})
            out = fn(**kwargs)
            size = len(out) if isinstance(out, list) else 1
            sp.set(**{"tmp.results": size})
            if name == "search_chunks":
                sp.output([{"chunk": h.chunk.chunk_id, "score": round(h.score, 4), "via": h.via} for h in out])
            elif isinstance(out, dict):
                sp.output(out)
            else:
                sp.output({"results": size})
        ev.calls.append({"tool": name, "args": args, "ms": round((time.perf_counter() - t0) * 1000, 1), "results": size})
        return out


def _words(text: str) -> set[str]:
    return {w for w in _TOKEN.findall(normalise(text)) if len(w) > 2}


def entities_for(reading: Reading, cfg: Config) -> list[str]:
    q = reading.standalone
    want = [e for e, rx in cfg.retrieval.entity_cues.items() if rx.search(q)]
    if reading.details.get("entity") in cfg.retrieval.entities and reading.details["entity"] not in want:
        want.insert(0, reading.details["entity"])
    return want


def _filtered(items: list[FactItem], reading: Reading, cfg: Config, roads: list[str]) -> list[FactItem]:
    """Narrow a long list to what the question names: a road, a day type, words of a risk row."""
    q = reading.standalone
    named = [r for r in roads if norm_name(r, cfg.facts) in norm_name(q, cfg.facts)]
    dtp = cfg.retrieval.day_type_facts.get(reading.details.get("day_type") or "")
    out = []
    for it in items:
        if it.entity == "road" and named:
            v = it.value("road_name")
            if v is None or str(v.value) not in named:
                continue
        if it.entity == "shift_window" and dtp:
            v = it.value("day_type")
            if v is None or facts_day_type(str(v.value), cfg.facts) != dtp:
                continue
        out.append(it)
    risks = [it for it in out if it.entity == "risk"]
    if len(risks) > 3:
        qw = _words(q)
        ranked = sorted(risks, key=lambda it: -len(qw & _words(" ".join(str(v.value) for v in it.values
                                                                         if isinstance(v.value, str)))))
        keep = {it.fact_id for it in ranked[:3]}
        out = [it for it in out if it.entity != "risk" or it.fact_id in keep]
    return out


def _search(t: Tools, ev: Evidence, reading: Reading, recipe: str, scope: str = "any",
            types: tuple[str, ...] | None = None) -> list[Hit]:
    if t.doc is None:
        return []
    r = t.cfg.retrieval
    tmd_ids: tuple[str, ...] = ()
    if reading.details.get("tmd"):
        tid, _ = find_sheet(t.facts, reading.details["tmd"])
        tmd_ids = (tid,) if tid else ()
    req = SearchRequest(t.doc.doc_id, reading.standalone, types if types is not None else r.chunk_types.get(recipe, ()),
                        scope, False, r.k.get(recipe, r.k["default"]), tmd_ids,
                        tuple(x for x in t.doc.roads if x.lower() in reading.standalone.lower()))
    return t.call(ev, "search_chunks", t.search.search, req=req, user=t.user)


def _attach_conflicts(t: Tools, ev: Evidence) -> None:
    ids = {c for it in ev.facts for c in it.conflict_ids}
    ev.conflicts += [c for c in t.facts.conflicts if c.conflict_id in ids
                     and c.conflict_id not in {x.conflict_id for x in ev.conflicts}]


def fact(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ents = entities_for(reading, t.cfg) or ["doc_card"]
    ev.entities = ents
    for e in ents:
        items = t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity=e)
        ev.facts += _filtered(items, reading, t.cfg, t.doc.roads if t.doc else [])
    ev.chunks = _search(t, ev, reading, "fact")
    return ev


def procedure(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ev.chunks = _search(t, ev, reading, "procedure")
    if not ev.chunks:
        ev.chunks = _search(t, ev, reading, "procedure", scope="generic", types=())
    sections = {p.split(".", 1)[0] for h in ev.chunks for p in h.chunk.field_paths if p.startswith("section:")}
    cite = Citer(t.facts, t.chunks)
    ev.facts += [fact_item(f, t.facts, cite) for f in t.facts.of("section")       # facts extracted from those sections
                 if f.fact_id in sections and any(n not in ("heading", "section_type") for n in f.fields)]
    for e in entities_for(reading, t.cfg):
        if e == "contact":
            ev.facts += t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity="contact")
    return ev


def _sheet_from_question(t: Tools, reading: Reading) -> str | None:
    if reading.details.get("tmd"):
        return reading.details["tmd"]
    q = f" {re.sub(r'[^a-z0-9]+', ' ', reading.standalone.lower())} "
    for s in t.facts.tmd_sheets:
        title = re.sub(r"[^a-z0-9]+", " ", s.title.lower()).strip()
        if title and f" {title} " in q:
            return s.tmd_id
    return None


def diagram(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    name = _sheet_from_question(t, reading)
    if name:
        res = t.call(ev, "get_tmd", get_tmd, facts=t.facts, chunks=t.chunks, tmd=name)
        ev.tmd.append(res)
        if res.get("found"):
            by_id = {c.chunk_id: c for c in (t.chunks.chunks if t.chunks else [])}
            ev.chunks = [Hit(by_id[cid], 1.0, "tmd") for cid in res["chunk_ids"] if cid in by_id]
            ev.facts += [it for it in get_facts(t.facts, t.chunks, t.cfg, "tmd_sheet") if it.fact_id.endswith(res["tmd_id"])]
            ev.facts += [it for it in get_facts(t.facts, t.chunks, t.cfg, "stage") if it.fact_id in res["stages"]]
            ev.conflicts += [c for c in t.facts.conflicts if c.conflict_id in res["conflicts"]]
            return ev
        ev.notes.append(f"no diagram matches {name!r}")
    ev.chunks = _search(t, ev, reading, "diagram")
    if not ev.chunks:                       # nothing on the sheets matches: say which sheets there are
        ev.facts += get_facts(t.facts, t.chunks, t.cfg, "tmd_sheet")
    return ev


def status(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ev.status = t.call(ev, "get_status", get_status, facts=t.facts, chunks=t.chunks)
    return ev


def calculation(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    q = reading.standalone
    for name, calc in t.cfg.retrieval.calculations.items():
        if not re.search(calc["when"], q):
            continue
        if reading.details.get("entity") and reading.details["entity"] != calc["entity"] and reading.method == "Q1":
            continue
        items = t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity=calc["entity"])
        items = _filtered(items, reading, t.cfg, t.doc.roads if t.doc else [])
        if calc.get("match_field"):
            named = [it for it in items if (v := it.value(calc["match_field"])) is not None
                     and norm_name(str(v.value), t.cfg.facts) in norm_name(q, t.cfg.facts)]
            items = named or items[:1]
        for it in items[:2]:
            names = set(re.findall(r"[A-Za-z_]\w*", calc["expression"])) - {"min", "max", "abs", "round"}
            values = {}
            for n in names:
                v = it.value(n)
                if v is None:
                    break
                values[n] = {"value": v.value, "unit": "date" if calc["result_unit"] == "days" else v.unit,
                             "source": it.source, "display": v.raw}
            else:
                stated = None
                if calc.get("stated") and (sv := it.value(calc["stated"])) is not None:
                    stated = {"value": sv.value, "unit": sv.unit or calc["result_unit"], "source": it.source}
                res = t.call(ev, "calculate", calculate, expression=calc["expression"], values=values,
                             result_unit=calc["result_unit"], stated=stated)
                res.update(name=name, what=it.label)
                ev.calcs.append(res)
                ev.facts.append(it)
        if ev.calcs:
            break
    if not ev.calcs:
        ev.notes.append("no stored values to calculate with for this question")
        ev.merge(fact(t, reading))
    return ev


def time_check_recipe(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    q = reading.standalone
    what = ("tsl" if t.cfg.retrieval.entity_cues["speed_limit"].search(q) else
            "stage" if re.search(r"(?i)\bstages?\b|\bparking\b|\bunattended\b", q) else "work")
    ent = {"tsl": "speed_limit", "stage": "stage", "work": "shift_window"}[what]
    items = t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity=ent)
    ev.facts += _filtered(items, reading, t.cfg, t.doc.roads if t.doc else [])
    ev.facts += get_facts(t.facts, t.chunks, t.cfg, "work_period")
    cite = Citer(t.facts, t.chunks)
    d = reading.details
    ev.times.append(t.call(ev, "time_check", time_check, facts=t.facts, cfg=t.cfg, cite=cite,
                           date_iso=d.get("date_iso"), time=d.get("time"), day_type=d.get("day_type"), what=what,
                           calendar=t.calendar))
    return ev


def across_tmps(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    q = reading.standalone
    roads = {r for d in t.store.docs(t.user) for r in d.roads}
    road = reading.details.get("road") or next((r for r in roads if norm_name(r, t.cfg.facts) in
                                                 norm_name(q, t.cfg.facts)), None)
    orgs = {o for d in t.store.docs(t.user) for o in d.organisations}
    org = reading.details.get("organisation") or next((o for o in orgs if len(o) > 4 and o.lower() in q.lower()), None)
    date = reading.details.get("date_iso")
    ev.tmps = t.call(ev, "find_tmps", find_tmps, store=t.store, user=t.user, cfg=t.cfg, road=road,
                     suburb=reading.details.get("suburb"), organisation=org, active_from=date, active_to=date)
    if reading.details.get("date_text") and not date:
        ev.notes.append(f"the dates '{reading.details['date_text']}' were not applied as a filter")
    return ev


def summary(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ev.facts = t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity="doc_card")
    ev.chunks = _search(t, ev, reading, "summary")
    ev.status = get_status(t.facts, t.chunks)
    return ev


def general_rule(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ev.chunks = _search(t, ev, reading, "general_rule", scope="generic")
    return ev


def version_change(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    assert t.doc is not None
    same = sorted((d for d in t.store.docs(t.user) if d.tmp_ref == t.doc.tmp_ref), key=lambda d: (d.version, d.written))
    if len(same) < 2:
        ev.notes.append(f"only one version of {t.doc.tmp_ref} is stored, so there is nothing to compare")
        return ev
    a, b = same[-2], same[-1]
    ev.versions = t.call(ev, "compare_versions", compare_versions, a=t.store.facts(a.doc_id, t.user),
                         b=t.store.facts(b.doc_id, t.user))
    ev.versions["labels"] = [f"V{a.version}", f"V{b.version}"]
    return ev


def baseline(t: Tools, reading: Reading) -> Evidence:
    ev = Evidence()
    ev.facts = t.call(ev, "get_facts", get_facts, facts=t.facts, chunks=t.chunks, cfg=t.cfg, entity="doc_card")
    ev.chunks = _search(t, ev, reading, "baseline")
    return ev


def out_of_scope(t: Tools, reading: Reading) -> Evidence:
    return Evidence(notes=["the question needs data that is not loaded (for example the official standard's tables)"])


RECIPES: dict[str, Callable[[Tools, Reading], Evidence]] = {
    "fact": fact, "procedure": procedure, "diagram": diagram, "status": status, "calculation": calculation,
    "time_check": time_check_recipe, "across_tmps": across_tmps, "summary": summary, "general_rule": general_rule,
    "version_change": version_change, "out_of_scope": out_of_scope,
}


def run_recipes(t: Tools, reading: Reading) -> Evidence:
    names = reading.types or ["baseline"]
    fns = [RECIPES.get(n, baseline) if n != "baseline" else baseline for n in names]
    ev = Evidence()
    if len(fns) == 1:
        ev.merge(fns[0](t, reading))
    else:
        if t.doc is not None:
            _ = t.facts, t.chunks                 # load once before the threads share them
        with ThreadPoolExecutor(max_workers=min(4, len(fns))) as pool:
            futures = [lg.submit_in_context(pool, fn, t, reading) for fn in fns]
            for fut in futures:
                ev.merge(fut.result())
    if t.doc is not None:
        _attach_conflicts(t, ev)
        ids = {c.conflict_id for c in ev.conflicts}
        ev.conflicts += [c for c in conflicts_for(t.facts, {it.fact_id for it in ev.facts}) if c.conflict_id not in ids]
    return ev
