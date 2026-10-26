"""Document context shared by every chunk (plan 4.3, 4.4): the TMP reference and its aliases, site, roads, parties,
work dates, approval, the one-line context sentence, and per-page form names and zones for breadcrumbs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...config import Config
from ...records import RecordStore, Status
from ..facts.common import record_texts
from ..facts.model import Facts
from ..models import ElementStore


@dataclass
class DocContext:
    doc_id: str
    tmp_ref: str
    tmp_ref_aliases: list[str] = field(default_factory=list)
    slug: str = ""
    site_address: str | None = None
    suburb: str | None = None
    road_names: list[str] = field(default_factory=list)
    activity: str | None = None
    activity_records: list[str] = field(default_factory=list)
    parties: dict[str, str] = field(default_factory=dict)
    work_start: str | None = None
    work_end: str | None = None
    approval_status: str | None = None
    line: str = ""
    forms: dict[int, str] = field(default_factory=dict)
    zones: dict[int, str] = field(default_factory=dict)
    printed: dict[int, int | None] = field(default_factory=dict)
    page_heights: dict[int, float] = field(default_factory=dict)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _value(facts: Facts, entity: str, fname: str) -> object:
    for f in facts.of(entity):
        fv = f.fields.get(fname)
        if fv is not None and fv.value is not None and fv.status != Status.CONFLICT:
            return fv.value
    return None


def build_context(facts: Facts, records: RecordStore, store: ElementStore, cfg: Config) -> DocContext:
    k = cfg.chunks
    doc = next(iter(facts.of("tmp_document")), None)
    ref = doc.fields.get("tmp_reference") if doc else None
    aliases: list[str] = []
    if ref is not None and ref.status == Status.CONFLICT and ref.versions:
        tmp_ref = str(ref.versions[0]["value"])                 # the most frequent spelling
        aliases = [str(v["value"]) for v in ref.versions[1:]]
    elif ref is not None and ref.value:
        tmp_ref = str(ref.value)
        aliases = [str(v["value"]) for v in ref.versions[1:]] if ref.versions else []
    else:
        tmp_ref = facts.doc_id.split(":")[-1][:12]
    ctx = DocContext(facts.doc_id, tmp_ref, aliases, slugify(tmp_ref).upper())
    for rec in sorted(records.records.values(), key=lambda r: (r.page, r.record_id)):
        for text, _ in record_texts(rec):
            m = k.site_pattern.search(text)
            if m:
                ctx.site_address = " ".join(m.group(1).split()).rstrip(" ,.")
                break
        if ctx.site_address:
            break
    roads = facts.of("road")
    ctx.road_names = [str(r.fields["road_name"].value) for r in roads if "road_name" in r.fields]
    suburb = _value(facts, "road", "suburb")
    ctx.suburb = str(suburb) if suburb else None
    mentioned = {m.source.record_id for m in facts.mentions}
    for rec in sorted(records.of_kind("section"), key=lambda r: (r.page, r.record_id)):
        path = " / ".join(str(p) for p in rec.data.get("path") or [])
        if k.activity_context.search(path) and rec.record_id not in mentioned and rec.data.get("level", 1) > 1:
            ctx.activity_records.append(rec.record_id)
            if ctx.activity is None:
                ctx.activity = str(rec.data.get("heading") or "").strip(" :").lower() or None
    roles: list[tuple[str, str]] = []          # (role, who): contacts by their roles, sign-off blocks by role
    for c in facts.of("contact"):
        who = c.fields.get("organisation") or c.fields.get("name")
        for role in (c.fields["roles"].value if "roles" in c.fields else []) or []:
            if who is not None and who.value:
                roles.append((str(role), str(who.value)))
    roles += [(s.role, s.organisation or s.name or "") for s in facts.signoffs if s.organisation or s.name]
    for party, rx in k.parties.items():
        hit = next((who for role, who in roles if rx.search(role)), None)
        if hit:
            ctx.parties[party] = hit
    start, end = _value(facts, "work_period", "start_date"), _value(facts, "work_period", "end_date")
    ctx.work_start, ctx.work_end = (str(start) if start else None), (str(end) if end else None)
    ctx.approval_status = facts.approval.status if facts.approval else None
    ctx.line = k.doc_context.format(activity=ctx.activity or "works", site=ctx.site_address or ctx.suburb or "site")
    for page, info in store.pages.items():
        ctx.forms[page] = info.form_name or ""
        ctx.zones[page] = str(info.zone)
        ctx.printed[page] = info.printed_page
        ctx.page_heights[page] = info.height_pt
    return ctx
