"""The read-only tools of plan 5.6 over the facts store: get_facts, get_status, get_tmd, find_tmps, compare_versions.

Every value goes out with its status, raw text, unit, pages, a citation (form, section, page) and the conflicts
that touch it, so the answer can be checked against exactly what was given. ``search_chunks`` is in ``hybrid``,
``calculate`` in ``calc``, ``time_check`` in ``timecheck`` and ``resolve_tmp`` in ``resolve``.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from dataclasses import field as _field
from typing import Any

from ..catalog import normalise
from ..chunks.model import ChunkSet
from ..config import Config
from ..facts.common import norm_name
from ..facts.model import Conflict, Fact, Facts, FactValue
from ..records import Status
from .details import TMD_REF
from .store import DocInfo, FactsStore, User, latest

_PAGE_TAIL = re.compile(r"\s+—\s+pages?\s+[\d, –-]+$")
LABEL_FIELDS = ("road_name", "day_type", "tmd_id", "title", "name", "category", "question", "heading", "speed_kmh")


def pages_text(pages: list[int]) -> str:
    """"p.3", "pp.3, 10", or "pp.1–24" for a run of three or more pages."""
    pages = sorted(set(pages))
    if not pages:
        return ""
    if len(pages) == 1:
        return f"p.{pages[0]}"
    runs: list[str] = []
    start = prev = pages[0]
    for p in [*pages[1:], None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        runs.append(f"{start}–{prev}" if prev - start >= 2 else ", ".join(str(x) for x in range(start, prev + 1)))
        if p is not None:
            start = prev = p
    return "pp." + ", ".join(runs)


class Citer:
    """(form, section, page) for a fact, from the breadcrumb of the chunk that states it (the same words the reader
    sees in the context); a fact no chunk states falls back to its pages."""

    def __init__(self, facts: Facts, chunks: ChunkSet | None):
        self.crumb: dict[str, str] = {}
        if chunks is None:
            return
        for c in chunks.chunks:
            first = c.header.split("\n", 1)[0]
            parts = [p.strip() for p in _PAGE_TAIL.sub("", first).split("›")][1:]   # drop the TMP reference
            crumb = ", ".join(parts)
            for path in c.field_paths:
                self.crumb.setdefault(path, crumb)
                self.crumb.setdefault(path.split(".", 1)[0], crumb)

    def __call__(self, fact: Fact | None, pages: list[int] | None = None, path: str | None = None) -> str:
        crumb = (self.crumb.get(path or "") or (self.crumb.get(fact.fact_id) if fact else None) or "")
        where = pages_text(pages if pages is not None else (fact.pages() if fact else []))
        return ", ".join(p for p in (crumb, where) if p)


@dataclass
class Value:
    path: str                           # "speed_limit:0.length_m"
    field: str
    value: Any
    unit: str | None = None
    raw: str | None = None
    status: str = "verified"
    pages: list[int] = _field(default_factory=list)
    conflict_ids: list[str] = _field(default_factory=list)
    versions: list[dict] = _field(default_factory=list)
    notes: list[str] = _field(default_factory=list)


@dataclass
class FactItem:
    fact_id: str
    entity: str
    label: str
    values: list[Value]
    source: str
    pages: list[int] = _field(default_factory=list)
    applicability: str = "active"
    flags: list[str] = _field(default_factory=list)
    conflict_ids: list[str] = _field(default_factory=list)

    def value(self, name: str) -> Value | None:
        return next((v for v in self.values if v.field == name), None)


def conflicts_for(facts: Facts, refs: set[str]) -> list[Conflict]:
    """Conflicts whose affected list or versions name any of ``refs`` (fact paths, fact ids or TMD ids)."""
    out = []
    for c in facts.conflicts:
        hit = any(a in refs or a.split(".", 1)[0] in refs for a in c.affected)
        hit = hit or any(v.get("mapped_to") in refs for v in c.versions)
        if hit:
            out.append(c)
    return out


def _value(path: str, name: str, fv: FactValue, facts: Facts) -> Value:
    cids = [c.conflict_id for c in conflicts_for(facts, {path})]
    pages = sorted({s.page for s in fv.sources})
    return Value(path, name, fv.value, fv.unit, fv.raw, str(fv.status), pages, cids, list(fv.versions), list(fv.notes))


def _label(f: Fact) -> str:
    bits = [str(f.fields[n].value) for n in LABEL_FIELDS if n in f.fields and f.fields[n].value not in (None, "")]
    return f"{f.entity}[{', '.join(bits[:2])}]" if bits else f.fact_id


def fact_item(f: Fact, facts: Facts, cite: Citer, fields: list[str] | None = None) -> FactItem:
    vals = [_value(f"{f.fact_id}.{n}", n, fv, facts) for n, fv in f.fields.items()
            if fields is None or n in fields]
    cids = list(dict.fromkeys(c for v in vals for c in v.conflict_ids))
    cids += [c.conflict_id for c in conflicts_for(facts, {f.fact_id}) if c.conflict_id not in cids]
    return FactItem(f.fact_id, f.entity, _label(f), vals, cite(f), f.pages(), f.applicability, list(f.flags), cids)


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


def tmd_items(facts: Facts, cite: Citer) -> list[FactItem]:
    out = []
    for s in facts.tmd_sheets:
        cids = [c.conflict_id for c in conflicts_for(facts, {s.tmd_id})]
        vals = [Value(f"tmd_sheet:{s.tmd_id}.{n}", n, v, pages=[s.page], conflict_ids=cids)
                for n, v in (("tmd_id", s.tmd_id), ("title", s.title), ("sheet", s.sheet), ("rev", s.rev))
                if v is not None]
        out.append(FactItem(f"tmd_sheet:{s.tmd_id}", "tmd_sheet", f"tmd_sheet[{s.tmd_id}, {s.title}]", vals,
                            cite(None, [s.page], f"tmd_sheet:{s.tmd_id}"), [s.page], conflict_ids=cids))
    return out


def get_facts(facts: Facts, chunks: ChunkSet | None, cfg: Config, entity: str, fields: list[str] | None = None,
              flt: dict[str, Any] | None = None) -> list[FactItem]:
    """Typed values of one entity (plan 5.6). ``entity`` is a tool entity (``retrieval.entities``)."""
    cite = Citer(facts, chunks)
    stored = cfg.retrieval.entities.get(entity, entity)
    if entity == "tmd_sheet":
        return tmd_items(facts, cite)
    if entity == "doc_card":
        items = [fact_item(f, facts, cite) for e in ("tmp_document", "work_period") for f in facts.of(e)]
        items += [fact_item(f, facts, cite, ["road_name", "suburb", "start_rp", "end_rp"]) for f in facts.of("road")]
        items += [fact_item(f, facts, cite, ["name", "organisation", "roles", "phone"]) for f in facts.of("contact")]
        return items + tmd_items(facts, cite)
    out = []
    for name in stored.split("|"):
        for f in facts.of(name):
            if flt and not _matches(f, flt, cfg):
                continue
            item = fact_item(f, facts, cite, fields)
            if item.values:
                out.append(item)
    return out


def get_status(facts: Facts, chunks: ChunkSet | None) -> dict:
    """Approval, every sign-off block, all conflicts, rating mismatches and the count of unverified values."""
    cite = Citer(facts, chunks)
    unverified = [f"{f.fact_id}.{n}" for f in facts.facts for n, fv in f.fields.items()
                  if fv.status == Status.UNVERIFIED]
    mismatches = [c for c in facts.checks if c.outcome == "flagged"]
    return {
        "approval": facts.approval.status if facts.approval else "unknown",
        "required": list(facts.approval.required) if facts.approval else [],
        "signed": list(facts.approval.signed) if facts.approval else [],
        "unsigned": list(facts.approval.unsigned) if facts.approval else [],
        "not_applicable": list(facts.approval.not_applicable) if facts.approval else [],
        "signoffs": [{"role": s.role, "status": s.status, "name": s.name, "organisation": s.organisation,
                      "signature": s.signature, "date": s.date, "page": s.page,
                      "source": cite(None, [s.page], None)} for s in facts.signoffs],
        "conflicts": [{"id": c.conflict_id, "title": c.title,
                       "versions": [{"value": v.get("value"), "pages": v.get("pages", []), "note": v.get("note")}
                                    for v in c.versions]} for c in facts.conflicts],
        "mismatches": [{"check": c.name, "detail": c.detail, "items": c.items} for c in mismatches],
        "unverified": len(unverified),
        "unverified_paths": unverified[:20],
    }


def _norm_tmd(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalise(text))


def find_sheet(facts: Facts, tmd: str) -> tuple[str | None, list[Conflict]]:
    """A diagram named by ID, number or title -> (TMD id, the conflicts about that name). A number another
    sheet's title claims (the stage lists' "TMD4") comes back with its conflicts, so the answer shows both."""
    want = _norm_tmd(tmd)
    by_alias = {_norm_tmd(a): s.tmd_id for s in facts.tmd_sheets for a in (*s.aliases, s.tmd_id, s.title)}
    tid = by_alias.get(want)
    m = TMD_REF.search(tmd)
    if tid is None and m:
        num = int(m.group(1))
        tid = next((s.tmd_id for s in facts.tmd_sheets if s.sheet.isdigit() and int(s.sheet) == num), None)
    if tid is None and want:
        hits = [s.tmd_id for s in facts.tmd_sheets if want in _norm_tmd(s.title) or _norm_tmd(s.title) in want]
        tid = hits[0] if len(hits) == 1 else None
    if tid is None:
        return None, []
    refs = {tid}
    if m:          # mentions written with this number but mapped elsewhere
        num = int(m.group(1))
        refs |= {x.mention_id for x in facts.mentions if x.number == num and x.id_conflict}
    return tid, conflicts_for(facts, refs)


def get_tmd(facts: Facts, chunks: ChunkSet | None, tmd: str) -> dict:
    """Everything about one diagram as text: overview, what the image shows, layout, distances, sign schedules, all its
    text, notes, title block and the pictures beside it (plan 5.6)."""
    tid, conflicts = find_sheet(facts, tmd)
    if tid is None:
        return {"tmd": tmd, "found": False, "sheets": [f"{s.tmd_id} {s.title} (p.{s.page})" for s in facts.tmd_sheets]}
    sheet = facts.sheet(tid)
    assert sheet is not None
    parts = []
    if chunks is not None:
        order = {"sheet_overview": 0, "sheet_image": 1, "sheet_layout": 2, "sheet_distances": 3, "sheet_sign_schedule": 4,
                 "sheet_text": 5, "sheet_notes": 6, "sheet_details": 7, "figure_caption": 8}
        parts = sorted((c for c in chunks.chunks if c.chunk_type in order and tid in (c.metadata.get("tmd_ids") or [])),
                       key=lambda c: (order[c.chunk_type], c.chunk_id))
    stages = [f for f in facts.of("stage") if "tmd_id" in f.fields and f.fields["tmd_id"].value == tid]
    return {"tmd": tmd, "found": True, "tmd_id": tid, "title": sheet.title, "page": sheet.page,
            "aliases": sheet.aliases, "chunk_ids": [c.chunk_id for c in parts],
            "stages": [f.fact_id for f in stages], "conflicts": [c.conflict_id for c in conflicts]}


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


def find_tmps(store: FactsStore, user: User, cfg: Config, road: str | None = None, suburb: str | None = None,
              organisation: str | None = None, active_from: str | None = None, active_to: str | None = None,
              approval_status: str | None = None, has_conflict: bool | None = None) -> list[DocInfo]:
    """TMPs (latest version each) the user may read that match every filter given."""
    def has(names: list[str], want: str | None) -> bool:
        return not want or any(norm_name(want, cfg.facts) in norm_name(n, cfg.facts) for n in names)
    out = []
    for d in latest(store.docs(user)):
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
    return sorted(out, key=lambda d: (d.work_start or "", d.tmp_ref))


def compare_versions(a: Facts, b: Facts) -> dict:
    """Changed fields and sections between two versions of one TMP (fact ids are stable within a template)."""
    def flat(f: Facts) -> dict[str, Any]:
        return {f"{x.fact_id}.{n}": fv.value for x in f.facts for n, fv in x.fields.items()}
    fa, fb = flat(a), flat(b)
    changed = [{"path": p, "from": fa.get(p), "to": fb.get(p)} for p in sorted(set(fa) | set(fb))
               if fa.get(p) != fb.get(p) and not p.startswith("section:")]
    heads = {"a": {str(s.fields["heading"].value) for s in a.of("section") if "heading" in s.fields},
             "b": {str(s.fields["heading"].value) for s in b.of("section") if "heading" in s.fields}}
    return {"changed": changed, "sections_added": sorted(heads["b"] - heads["a"]),
            "sections_removed": sorted(heads["a"] - heads["b"]),
            "approval": [a.approval.status if a.approval else None, b.approval.status if b.approval else None]}
