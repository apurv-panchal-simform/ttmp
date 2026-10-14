"""Step 4, assemble the context (plan 5.10): facts, then calculations, then chunks (best first), then the conflicts
on the values used. Every block carries its status and source. The budget is in tokens; chunks are cut from the
bottom of the ranking first, never facts, calculations or conflicts."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..chunks.tokens import count
from ..facts.model import Conflict
from .classify import Reading
from .recipes import Evidence
from .store import DocInfo
from .tools import FactItem, Value, pages_text

_RANK = {"conflict": 3, "unverified": 2, "not_applicable": 1, "reviewed": 0, "verified": 0}


def page_span(pages: list[int]) -> str:
    return pages_text(pages)


def show(value: Any) -> str:
    if isinstance(value, bool):
        return "ticked" if value else "not ticked"
    if value is None:
        return "blank"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list | dict):
        if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
            return ", ".join(str(v) for v in value)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def conflict_line(c: Conflict) -> str:
    vers = "; ".join(f"\"{v.get('value')}\" ({page_span(v.get('pages') or [])})" for v in c.versions)
    return f"{c.conflict_id} {c.title}: {vers}"


def versions_text(v: Value) -> str:
    return "; ".join(f"\"{x.get('value')}\" ({page_span(x.get('pages') or [])})" for x in v.versions)


def _value_text(v: Value) -> str:
    if v.status == "conflict" and v.value is None and v.versions:
        return f"{v.field}: the document gives different values: {versions_text(v)} [conflict: {', '.join(v.conflict_ids)}]"
    text = f"{v.field} {show(v.value)}" + (f" {v.unit}" if v.unit and v.value is not None else "")
    raw = (v.raw or "").strip()
    if raw and raw != show(v.value) and len(raw) <= 60 and raw.lower() not in v.field.replace("_", " "):
        text += f" (written \"{raw}\")"
    if v.status != "verified":
        text += f" [{v.status}" + (f": {', '.join(v.conflict_ids)}" if v.conflict_ids else "") + "]"
    return text


def fact_block(it: FactItem) -> str:
    worst = max((v.status for v in it.values), key=lambda s: _RANK.get(s, 0), default="verified")
    extra = " · struck through (not applicable)" if it.applicability == "not_applicable" else ""
    conf = f" · conflicts: {', '.join(it.conflict_ids)}" if it.conflict_ids else ""
    return (f"- {it.label}: " + " · ".join(_value_text(v) for v in it.values)
            + f"\n  [status: {worst}{extra}{conf} · source: {it.source}]")


def _calc_block(c: dict) -> str:
    if "error" in c:
        return f"- {c.get('name', 'calculate')}: not calculated ({c['error']})"
    cross = f" · {c['cross_check']}" if c.get("cross_check") else ""
    return (f"- {c.get('name', 'calculate')} ({c.get('what', '')}): result {c['result']} {c['unit']} · working: "
            f"{c['working']}{cross}\n  [sources: {'; '.join(c.get('sources') or [])}]")


def _yes(v: Any) -> str:
    return "yes" if v is True else "no" if v is False else "not certain"


def _time_block(t: dict) -> str:
    head = f"- time_check ({t.get('what')}"
    head += f", {t['date']}" if t.get("date") else ""
    head += f", {t['time']}" if t.get("time") else ""
    lines = [head + f"): allowed={_yes(t.get('allowed'))} · day type: {t.get('day_type')}"
             + (f" · window: {t['window']}" if t.get("window") else "") + f" · {t.get('reason', '')}"]
    for p in t.get("per_day_type") or []:
        lines.append(f"  · if {p['day_type']}: allowed={_yes(p['allowed'])} · {p['window']} · {p['reason']}")
    for s in t.get("stages") or []:
        lines.append(f"  · stage {s['stage']}: allowed={_yes(s['allowed'])} · {s['reason']} ({s['source']})")
    lines.append(f"  [sources: {'; '.join(x for x in t.get('sources') or [] if x)}]")
    return "\n".join(lines)


def _status_block(s: dict) -> str:
    def who(o: dict) -> str:
        bits = [x for x in (o.get("name"), o.get("organisation")) if x]
        sig = f"signature \"{o['signature']}\"" if o.get("signature") else "signature blank"
        date = f"date {o['date']}" if o.get("date") else "date blank"
        return f"{o['role']}: {', '.join(bits) or 'no name'} · {sig} · {date} ({o['source']})"
    lines = [f"- approval: {s['approval']} (required sign-offs: {', '.join(s['required']) or 'none listed'})"]
    for o in s["signoffs"]:
        label = {"signed": "signed", "unsigned": "not signed",
                 "not_applicable": "struck through (not applicable)"}.get(o["status"], o["status"])
        lines.append(f"- {label}: {who(o)}")
    for c in s["conflicts"]:
        vers = "; ".join(f"\"{v['value']}\" ({page_span(v.get('pages') or [])})" for v in c["versions"])
        lines.append(f"- conflict {c['id']} {c['title']}: {vers}")
    for m in s["mismatches"]:
        lines.append(f"- rating mismatch ({m['check']}): {m['detail']}")
    lines.append(f"- unverified values: {s['unverified']}")
    return "\n".join(lines)


@dataclass
class Context:
    text: str                                              # what Q3/Q4 see after their QUESTION line
    tokens: int
    pages: set[int] = field(default_factory=set)
    values: list[tuple[Value, FactItem]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    generic: list[str] = field(default_factory=list)       # chunk text from the contractor's general standard
    struck: list[str] = field(default_factory=list)        # chunk text from struck-through sections
    chunk_ids: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    empty: bool = False                                    # nothing at all answers the question
    question: str = ""                                     # the standalone question (its numbers are not invented)


def assemble(reading: Reading, doc: DocInfo | None, ev: Evidence, budget: int) -> Context:
    head: list[str] = []                # the prompts put the QUESTION line first themselves (Q3, Q4)
    if doc is not None:
        version = f"V{doc.version}"
        where = doc.site_address or ", ".join(doc.roads[:1])
        head.append(f"TMP: {doc.tmp_ref} · {where} · version {version} · approval: {doc.approval_status or 'unknown'}")
    pages: set[int] = set()
    fixed: list[str] = []
    if ev.facts:
        fixed.append("FACTS\n" + "\n".join(fact_block(it) for it in ev.facts))
        pages |= {p for it in ev.facts for p in it.pages} | {p for it in ev.facts for v in it.values for p in v.pages}
    calc = [_calc_block(c) for c in ev.calcs] + [_time_block(t) for t in ev.times]
    if calc:
        fixed.append("CALCULATIONS\n" + "\n".join(calc))
    if ev.status:
        fixed.append("STATUS\n" + _status_block(ev.status))
        pages |= {o["page"] for o in ev.status["signoffs"]}
        pages |= {p for c in ev.status["conflicts"] for v in c["versions"] for p in v.get("pages") or []}
    for t in ev.tmd:
        if t.get("found"):
            fixed.append(f"DIAGRAM\n- {t['tmd_id']} \"{t['title']}\" (p.{t['page']}); also written: "
                         f"{', '.join(t['aliases'])}" + (f"; conflicts: {', '.join(t['conflicts'])}" if t['conflicts'] else ""))
            pages.add(t["page"])
        elif "sheets" in t:
            fixed.append(f"DIAGRAM\n- no sheet matches \"{t['tmd']}\"; sheets: {'; '.join(t['sheets'])}")
    if ev.tmps:
        fixed.append("TMPS\n" + "\n".join(
            f"- {d.tmp_ref} · {d.site_address or ', '.join(d.roads)} · {d.work_start or '?'} to {d.work_end or '?'} · "
            f"approval {d.approval_status or 'unknown'}" + (" · has conflicts" if d.has_conflict else "") for d in ev.tmps))
    if ev.versions:
        a, b = ev.versions.get("labels", ["A", "B"])
        rows = [f"- {c['path']}: {show(c['from'])} ({a}) -> {show(c['to'])} ({b})" for c in ev.versions["changed"][:40]]
        rows += [f"- section added in {b}: {x}" for x in ev.versions["sections_added"]]
        rows += [f"- section removed in {b}: {x}" for x in ev.versions["sections_removed"]]
        fixed.append("VERSIONS\n" + ("\n".join(rows) or "- no differences"))
    tail = []
    if ev.conflicts:
        tail.append("CONFLICTS ON VALUES USED\n" + "\n".join(f"- {conflict_line(c)}" for c in ev.conflicts))
        pages |= {p for c in ev.conflicts for v in c.versions for p in v.get("pages") or []}
    if ev.notes:
        tail.append("NOT CHECKED\n" + "\n".join(f"- {n}" for n in ev.notes))

    def chunk_block(i: int, h: Any) -> str:
        c, md = h.chunk, h.chunk.metadata
        crumb = c.header.split("\n", 1)[0]
        scope = md.get("scope", "site_specific")
        tags = [f"status: {md.get('status', 'verified')}",
                "scope: generic (the contractor's general standard, not specific to this site)" if scope == "generic"
                else f"scope: {scope}"]
        if md.get("applicability") == "not_applicable":
            tags.append("struck through (not applicable)")
        if md.get("conflict_ids"):
            tags.append(f"conflicts: {', '.join(md['conflict_ids'])}")
        return f"[{i}] {crumb} [{' · '.join(tags)}]\n{c.content}"

    hits = list(ev.chunks)
    dropped: list[str] = []
    while True:
        blocks = [chunk_block(i, h) for i, h in enumerate(hits, 1)]
        parts = head + fixed + (["CHUNKS\n" + "\n\n".join(blocks)] if blocks else ["CHUNKS\n(none)"]) + tail
        text = "\n".join(parts)
        tokens = count(text)
        if tokens <= budget or not hits:
            break
        dropped.append(hits.pop().chunk.chunk_id)
    for h in hits:
        md = h.chunk.metadata
        pages |= set(md.get("pages") or ([md["page"]] if md.get("page") else []))
    from .check import _cited_pages  # pages the context text itself names
    pages |= set(_cited_pages(text))
    values = [(v, it) for it in ev.facts for v in it.values]
    generic = [h.chunk.content for h in hits if h.chunk.metadata.get("scope") == "generic"]
    struck = [h.chunk.content for h in hits if h.chunk.metadata.get("applicability") == "not_applicable"]
    empty = not (ev.facts or ev.calcs or ev.times or ev.status or ev.tmps or ev.versions or hits or ev.notes
                 or any(t.get("found") for t in ev.tmd))
    return Context(text, tokens, {p for p in pages if isinstance(p, int)}, values, list(ev.conflicts), generic, struck,
                   [h.chunk.chunk_id for h in hits], dropped, empty, reading.standalone)
