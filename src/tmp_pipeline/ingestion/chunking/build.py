"""Chunk builders (plan 4.1): one answerable unit per chunk, built from the canonical record (facts), the handler
records and the element store, never from pages. Records, step lists and sign runs are never split; a narrative
section over the hard maximum is split at its level-2 sections, then bullets, then sentences.

Every builder states which fact paths, records and elements its chunk carries; the lineage check (``lineage.py``)
relies on that. Builders run in a fixed order and each record is rendered once by the first builder that owns it,
except that facts may also be summarised in the document card.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ...calc import calculate, named_values
from ...config import Config
from ...records import Record, RecordStore, Status
from ..facts.model import Fact, Facts, FactValue, Source
from ..models import ElementStore
from ..vocab import Outcome
from . import size, tokens
from .context import DocContext, slugify
from .lineage import Corpus
from .model import Chunk
from .text import field_lines, header, label, pages_text, plain, value_text

ROLE_NAMES = {"ttm_designer": "TTM designer", "design_checker": "Design checker", "activity_manager": "Activity manager",
              "rca_primary": "RCA review, primary network", "rca_other": "RCA review, other network",
              "rca_authorisation_form3": "RCA authorisation (Form 3)"}


def _table_rows(rec: Record) -> list[tuple[str | None, str]]:
    """A table-like record as (group, line) with one line per table row: the label/value pairs of a row side by side
    ("First Sign In: 0900 hrs | Commence Pick up: 1430 hrs"), else the row's cells left to right. A row is never
    cut, so a label always stays with its value."""
    d = rec.data
    pairs = d.get("pairs") or []
    if pairs:
        rows: dict[tuple, list[dict]] = {}
        for n, p in enumerate(pairs):
            key = (p["row"], p.get("line", 0)) if p.get("row") is not None else (n, 0)   # older records: no row
            rows.setdefault(key, []).append(p)
        out = []
        for key in sorted(rows):
            ps = rows[key]
            line = " | ".join(f"{p.get('label') or ''}: {p.get('value') or ''}".strip(": ") for p in ps)
            out.append((ps[0].get("group"), line))
        return out
    cells = [c for c in d.get("cells") or [] if str(c.get("text") or "").strip()]
    if cells:
        by_row: dict[int, list[dict]] = {}
        for c in cells:
            by_row.setdefault(int(c.get("row", 0)), []).append(c)
        return [(None, " | ".join(" ".join(str(c["text"]).split()) for c in sorted(cs, key=lambda c: c.get("col", 0))))
                for _, cs in sorted(by_row.items())]
    return []


def _grouped(rows: list[tuple[str | None, str]]) -> list[tuple[str | None, list[str]]]:
    """Consecutive rows under one group heading, in table order."""
    out: list[tuple[str | None, list[str]]] = []
    for group, line in rows:
        if out and out[-1][0] == group:
            out[-1][1].append(line)
        else:
            out.append((group, [line]))
    return out


def _text(rec: Record) -> str:
    """The text a table-like record holds, one line per table row under its group headings, else its field values."""
    rows = _table_rows(rec)
    if rows:
        lines: list[str] = []
        for group, rs in _grouped(rows):
            lines += ([f"{group.rstrip(':')}:"] if group else []) + rs
        return "\n".join(lines)
    return "\n".join(f"{label(k)}: {fv.raw if fv.raw is not None else plain(fv.value)}"
                     for k, fv in rec.fields.items() if fv.value is not None or fv.raw)


_ID = re.compile(r"\s*[(\[]?(?:IDs?:\s*)?(?:p\d+:[a-z_]+:\d+(?:,\s*)?)+[)\]]?")


_PLAIN_LABELS = {"dimension_label", "rp_label", "road_label", "house_label", "place_label", "callout"}


def _full_lines(full: object) -> list[str]:
    """Extra lines from a complete picture read (X13): what it shows, its text, what could not be read."""
    if not isinstance(full, dict):
        return []

    def mark(x: dict) -> str:
        return "" if x.get("ok") else " (unconfirmed)"
    texts = full.get("texts") or {}
    out = [f"{head}: {texts[k]['text']}{mark(texts[k])}" for k, head in
           (("drawing_type", "Type of picture"), ("purpose", "Purpose"), ("pedestrians", "Pedestrians"),
            ("vehicles", "Vehicles")) if k in texts]
    out += [f"Shows ({x.get('where') or 'position not stated'}): {x.get('looks_like')}{mark(x)}"
            for x in full.get("items") or [] if x.get("looks_like")]
    out += [f"Could not be read: {u.get('what')} ({u.get('where')})" for u in full.get("unreadable") or []]
    out += [f"Inferred, not read from the picture: {x}" for x in full.get("inferred") or []]
    return out


def _sheet_summary(summary: object) -> list[str]:
    """Lines for the sheet overview from the model's reading of the layout (X8). Each claim that did not pass the
    checks is marked unconfirmed; the symbol ids it cites are dropped from the text (they stay in the record)."""
    if isinstance(summary, str):
        return [f"Summary: {summary}"] if summary else []
    if not isinstance(summary, dict):
        return []
    if summary.get("text"):
        return [f"Summary: {summary['text']}"]
    claims = summary.get("claims") or {}

    def mark(c: dict) -> str:
        return "" if c.get("ok") else " (unconfirmed)"

    def clean(text: object) -> str:
        return _ID.sub("", str(text or "")).strip().rstrip(".")
    lines: list[str] = []
    for c in claims.get("layout") or []:
        if clean(c.get("statement")):
            lines.append(f"Layout: {clean(c.get('statement'))}.{mark(c)}")
    for c in claims.get("approaches") or []:
        text = ". ".join(x for x in (clean(c.get("direction")), clean(c.get("notes"))) if x)
        if text:
            lines.append(f"Approach: {text}.{mark(c)}")
    for c in claims.get("pedestrians") or []:
        if clean(c.get("statement")):
            lines.append(f"Pedestrians: {clean(c.get('statement'))}.{mark(c)}")
    for u in summary.get("unclear") or []:
        if clean(u):
            lines.append(f"Not clear from the drawing: {clean(u)}.")
    return lines


def _num(value: object) -> float:
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def _by_value(rows: list[tuple[str, dict[str, FactValue]]], columns: list[tuple[str, str]], order: dict[str, float],
              sep: str = ", ") -> list[str]:
    """Column views: the rows grouped by the value of each column, "Initial risk Very High (11): Work area, Stop Go …".
    Values are the stored ones (a conflict is written out, an unverified value marks its row "(unconfirmed)"); the
    counts are counted here, in code, so an answer copies them and never counts. Higher ``order`` comes first; values
    it does not know keep the order they were met in, last."""
    out = []
    for key, name in columns:
        groups: dict[str, list[str]] = {}
        for title, fields in rows:
            fv = fields.get(key)
            if fv is None or fv.value in (None, ""):
                continue
            value = value_text(fv) if fv.status == Status.CONFLICT else plain(fv.value)
            groups.setdefault(value, []).append(title + (" (unconfirmed)" if fv.status == Status.UNVERIFIED else ""))
        ranked = sorted(groups.items(), key=lambda kv: -order.get(kv[0].lower(), -1.0))
        out += [f"{name} {value} ({len(titles)}): {sep.join(titles)}" for value, titles in ranked]
    return out


def _pair(a: FactValue | None, b: FactValue | None, joiner: str, order: dict[str, float]) -> FactValue | None:
    """Two stored values as one, for the combinations people ask about ("Very High → Medium"); None if either is
    missing or in conflict (the conflict is written out in the single-column lines)."""
    if a is None or b is None or a.value in (None, "") or b.value in (None, "") or Status.CONFLICT in (a.status, b.status):
        return None
    va, vb = plain(a.value), plain(b.value)
    value = f"{va}{joiner}{vb}"
    order.setdefault(value.lower(), order.get(va.lower(), 0.0) * 10 + order.get(vb.lower(), 0.0))
    unverified = Status.UNVERIFIED in (a.status, b.status)
    return FactValue(value, status=Status.UNVERIFIED if unverified else Status.VERIFIED)


@dataclass
class _Item:
    """One whole block of text with where it came from; blocks are packed into chunks, never cut."""
    text: str
    pages: list[int]
    paths: list[str] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    elements: list[str] = field(default_factory=list)


class Builder:
    def __init__(self, facts: Facts, records: RecordStore, store: ElementStore, cfg: Config, ctx: DocContext):
        self.facts, self.records, self.store, self.cfg, self.ctx = facts, records, store, cfg, ctx
        self.k = cfg.chunks
        self.chunks: list[Chunk] = []
        self.used: set[str] = set()
        self._ids: set[str] = set()
        self._heads = sorted(
            ((r.page, self._top(r), [str(p) for p in r.data.get("path") or []]) for r in records.of_kind("section")),
            key=lambda h: (h[0], -h[1]))

    # --- helpers -------------------------------------------------------------------------------------------------
    def _top(self, rec: Record) -> float:
        for eid in rec.element_ids():
            el = self.store.elements.get(eid)
            if el is not None:
                return el.bbox_pt[3]
        return 0.0

    def path_for(self, rec: Record) -> list[str]:
        """The heading path a record sits under: the nearest section heading above it, else the last one before."""
        if rec.kind == "section":
            return [str(p) for p in rec.data.get("path") or []]
        top = self._top(rec)
        same = [h for h in self._heads if h[0] == rec.page and h[1] >= top]
        if same:
            return min(same, key=lambda h: h[1])[2]
        earlier = [h for h in self._heads if h[0] < rec.page]
        return earlier[-1][2] if earlier else []

    def emit(self, ctype: str, key: str, path: list[str], pages: Iterable[int], content: str, *,
             facts: Iterable[Fact] = (), paths: Iterable[str] = (), records: Iterable[Record] = (),
             elements: Iterable[str] = (), meta: dict | None = None, parent: str | None = None,
             splittable: bool = True) -> list[Chunk]:
        facts, records = list(facts), list(records)
        sources: list[Source] = [s for f in facts for s in f.sources]
        sources += [s for f in facts for fv in f.fields.values() for s in fv.sources]
        record_ids = list(dict.fromkeys([*(s.record_id for s in sources if s.record_id),
                                         *(r.record_id for r in records)]))
        element_ids = list(dict.fromkeys([*(e for s in sources for e in s.element_ids),
                                          *(e for r in records for e in r.element_ids()), *elements]))
        field_paths = list(dict.fromkeys([*(f"{f.fact_id}.{k}" for f in facts for k in f.fields), *paths]))
        # page furniture (the TMP number printed in every header) has no record: it says where a value is repeated,
        # not where the chunk's text is, so it never decides a chunk's pages
        pages = sorted(set(pages) or {s.page for s in sources if s.record_id} or {r.page for r in records})
        base = f"{self.ctx.slug}:{ctype}:{slugify(key)}"
        n = 2
        while base in self._ids:
            base = f"{self.ctx.slug}:{ctype}:{slugify(key)}-{n}"
            n += 1
        self._ids.add(base)
        head = header(self.ctx, self.k.breadcrumb_sep, pages, path)
        budget = self.k.hard_max - tokens.count(head) - 12
        parts = size.split(content, budget, self.k.split_overlap_sentences) if splittable else [content]
        out = []
        for i, text_ in enumerate(parts, 1):
            cid = base if len(parts) == 1 else f"{base}:part-{i}"
            label_ = "" if len(parts) == 1 else f" (part {i} of {len(parts)})"
            c = Chunk(cid, ctype, header(self.ctx, self.k.breadcrumb_sep, pages, path, label_), text_.strip(),
                      dict(meta or {}, pages=pages, section_path=path), field_paths, record_ids, element_ids,
                      parent_id=parent)
            out.append(c)
        if len(out) > 1:
            for c in out:
                c.siblings = [o.chunk_id for o in out if o is not c]
        self.chunks += out
        self.used.update(r.record_id for r in records)
        self.used.update(record_ids)
        return out

    def emit_packed(self, ctype: str, key: str, path: list[str], items: list[_Item]) -> list[Chunk]:
        """Pack whole blocks into chunks of at most ``target_max`` tokens (a block is never cut). Each chunk carries only
        the pages, fact paths, records and elements of the blocks it holds; chunks of one group are siblings."""
        packs = size.pack_indices([i.text for i in items], self.k.target_max, 0)
        out: list[Chunk] = []
        for n, idx in enumerate(packs):
            its = [items[i] for i in idx]
            out += self.emit(ctype, key if n == 0 else f"{key}-{n + 1}", path, sorted({p for i in its for p in i.pages}),
                             "\n".join(i.text for i in its), paths=[p for i in its for p in i.paths],
                             records=[r for i in its for r in i.records], elements=[e for i in its for e in i.elements],
                             splittable=False)
        if len(out) > 1:
            for c in out:
                c.siblings = [o.chunk_id for o in out if o is not c]
        return out

    def merge_small(self) -> None:
        """Merge clutter into one chunk per page (checkboxes, small form tables) or per table (rows of one table), so a
        question meets one coherent chunk instead of several fragments that each cost a header. Fact-backed chunks
        and entity chunks (road, schedule, sign-off ...) stay as they are: each answers one question precisely."""
        groups: dict[tuple, list[Chunk]] = {}
        for c in self.chunks:
            if c.field_paths or c.siblings or len(c.metadata["pages"]) != 1 or c.metadata.get("table_group"):
                continue              # a table group (DAY SHIFT (during School days)) answers one question: kept whole
            page, sp = c.metadata["pages"][0], c.metadata["section_path"]
            if c.chunk_type == "form_fields" and tokens.count(c.content) < self.k.merge_below:
                groups.setdefault(("form_fields", page), []).append(c)
            elif c.chunk_type == "narrative_table_row":
                rec = self.record(c.record_ids[0]) if c.record_ids else None
                groups.setdefault(("narrative_table_row", page, tuple(sp[:-1]), rec.data.get("table") if rec else None),
                                  []).append(c)
        for key, members in groups.items():
            if len(members) < 2:
                continue
            ctype, page = key[0], key[1]
            paths = [m.metadata["section_path"] for m in members]
            common: list[str] = []
            for parts in zip(*paths):
                if len(set(parts)) != 1:
                    break
                common.append(parts[0])
            if not common:            # no heading in common: these are different topics, not one form section
                continue
            items = []
            for m in members:
                text = m.content
                rest = m.metadata["section_path"][len(common):] if ctype == "form_fields" else []
                title = " › ".join(rest)
                if title and not text.lower().startswith(title.lower()[:30]):
                    text = f"{title}\n{text}"
                items.append(_Item(text, [page], [], [r for r in (self.record(i) for i in m.record_ids) if r is not None],
                                   list(m.element_ids)))
            at = self.chunks.index(members[0])
            for m in members:
                self.chunks.remove(m)
                self._ids.discard(m.chunk_id)
            before = len(self.chunks)
            name = f"page-{page}" if ctype == "form_fields" else f"p{page}-{common[-1] if common else 'table'}"
            self.emit_packed(ctype, name, common or ["Form fields"], items)
            new = self.chunks[before:]
            del self.chunks[before:]
            self.chunks[at:at] = new

    def record(self, rid: str) -> Record | None:
        return self.records.records.get(rid)

    def sheet_title(self, tmd_id: str) -> str:
        sh = self.facts.sheet(tmd_id)
        return f"{tmd_id} {sh.title.title()}" if sh else tmd_id

    # --- builders ------------------------------------------------------------------------------------------------
    def build(self) -> list[Chunk]:
        for step in (self.doc_card, self.stakeholders, self.activity, self.roads, self.schedules, self.speed_limits,
                     self.stages, self.signoffs, self.vulnerable_users, self.yes_no_questions,
                     self.form_answers_summary, self.risk_matrix, self.register_rows, self.register_summary,
                     self.procedures, self.sheets, self.figures, self.standard_rules,
                     self.narrative_sections, self.form_fields, self.narrative_table_rows, self.other_sections,
                     self.merge_small):
            step()
        return self.chunks

    def doc_card(self) -> None:
        c, f = self.ctx, self.facts
        lines = [f"TMP reference: {c.tmp_ref}" + (f" (also written {', '.join(c.tmp_ref_aliases)})"
                                                     if c.tmp_ref_aliases else "")]
        doc = next(iter(f.of("tmp_document")), None)
        if doc and "order_reference" in doc.fields:
            lines.append(f"Order reference: {value_text(doc.fields['order_reference'], f)}")
        if doc and "tmp_reference_printed" in doc.fields:
            lines.append(f"TMP reference as printed in the page header: {value_text(doc.fields['tmp_reference_printed'], f)}")
        if doc and "rca_reference" in doc.fields:
            lines.append(f"RCA consent / contract reference: {value_text(doc.fields['rca_reference'], f)}")
        if c.site_address:
            lines.append(f"Site: {c.site_address}")
        if c.activity:
            lines.append(f"Activity: {c.activity}")
        for r in f.of("road"):
            fl = r.fields
            ext = (f", RP {value_text(fl['start_rp'], f)}–{value_text(fl['end_rp'], f)}"
                   if "start_rp" in fl and "end_rp" in fl else "")
            lines.append(f"Road: {value_text(fl['road_name'], f) if 'road_name' in fl else '?'}{ext}")
        if c.parties:
            lines.append("Parties: " + "; ".join(f"{k} {v}" for k, v in c.parties.items()))
        if c.work_start or c.work_end:
            lines.append(f"Work dates: {c.work_start or '?'} to {c.work_end or '?'}")
            lines += [ln for wp in f.of("work_period") for ln in self.derived_lines(wp)]
        for sl in f.of("speed_limit"):      # the document card always travels with the question: the worked length is on it
            kmh = value_text(sl.fields["speed_kmh"], f) if "speed_kmh" in sl.fields else "temporary"
            lines += [f"{kmh} zone: {ln}" for ln in self.derived_lines(sl)]
        if f.tmd_sheets:
            lines.append("Diagrams: " + "; ".join(f"{s.tmd_id} {s.title.title()} (page {s.page})" for s in f.tmd_sheets))
        if f.approval:
            a = f.approval
            lines.append(f"Approval: {a.status.replace('_', ' ')}" +
                         (f"; signed: {', '.join(ROLE_NAMES.get(r, r) for r in a.signed)}" if a.signed else "") +
                         (f"; not signed: {', '.join(ROLE_NAMES.get(r, r) for r in a.unsigned)}" if a.unsigned else ""))
        if f.conflicts:
            lines.append("Conflicts in the document:")
            for cf in f.conflicts:
                vs = " / ".join(f"{plain(v.get('value'))} ({pages_text(v.get('pages', []))})" for v in cf.versions)
                lines.append(f"- {cf.conflict_id} {cf.title}: {vs}")
        self.emit("doc_card", "card", ["Document"], [1], "\n".join(lines), facts=f.of("tmp_document"),
                  meta={"conflict_ids": [cf.conflict_id for cf in f.conflicts]}, splittable=False)

    def stakeholders(self) -> None:
        contacts = self.facts.of("contact")
        if not contacts:
            return
        lines = []
        for ct in contacts:
            fl = ct.fields
            bits = [value_text(fl[k], self.facts) for k in ("name", "organisation", "phone", "email") if k in fl]
            roles = fl["roles"].value if "roles" in fl else []
            lines.append(f"{' — '.join(bits)} ({', '.join(ROLE_NAMES.get(r, r) for r in roles)})")
        self.emit("stakeholders", "parties", ["Stakeholders"], [p for c in contacts for p in c.pages()],
                  "\n".join(lines), facts=contacts)

    def _section_facts(self, rids: set[str]) -> list[Fact]:
        return [f for f in self.facts.of("section") if any(s.record_id in rids for s in f.sources)]

    def activity(self) -> None:
        recs = [r for r in (self.record(i) for i in self.ctx.activity_records) if r is not None]
        if not recs:
            return
        content = "\n\n".join(f"{r.data.get('heading', '').strip()}\n{r.data.get('text', '')}".strip() for r in recs)
        self.emit("activity", self.ctx.activity or "activity", self.path_for(recs[0])[:1] + ["Activity"],
                  [p for r in recs for p in r.data.get("pages", [r.page])], content,
                  facts=self._section_facts({r.record_id for r in recs}), records=recs)

    def derived_lines(self, fact: Fact) -> list[str]:
        """Figures worked out in code from a fact's stored values (``[chunks.derived]``), written with their working, e.g.
        "Length from the RPs: 0.615 km − 0.465 km = 0.150 km = 150 m (matches the stated 150 m)". A value in conflict or
        missing gives no line; an unconfirmed input makes the line unconfirmed."""
        out = []
        for d in self.k.derived.values():
            if d.get("entity") != fact.entity:
                continue
            st = fact.fields.get(d.get("stated", ""))
            stated = {"value": st.value, "unit": st.unit or d.get("unit", "")} if st and isinstance(st.value, int | float) else None
            res = calculate(d["expression"], named_values(fact.fields), d.get("unit"), stated)
            if "error" in res:
                continue
            unconfirmed = any(str(fv.status) == "unverified" for fv in fact.fields.values() if fv.value is not None)
            out.append(f"{d.get('label', 'Calculated')}: {res['working']}" + (f" ({res['cross_check']})" if res.get("cross_check") else "")
                       + (" (unconfirmed)" if unconfirmed else ""))
        return out

    def roads(self) -> None:
        for r in self.facts.of("road"):
            name = plain(r.fields["road_name"].value) if "road_name" in r.fields else r.fact_id
            lines = [*field_lines(r.fields, self.facts), *self.derived_lines(r)]
            self.emit("road", name, ["Roads", name], r.pages(), "\n".join(lines), facts=[r],
                      meta={"road_names": [name]})

    def schedules(self) -> None:
        for w in self.facts.of("shift_window"):
            day = plain(w.fields["day_type"].value) if "day_type" in w.fields else w.fact_id
            order = ("first_sign_in", "tc_in", "pick_up", "site_cleared")
            names = {"first_sign_in": "First sign in", "tc_in": "Traffic control in", "pick_up": "Commence pick up",
                     "site_cleared": "Site cleared"}
            lines = [f"Shift times ({day}):"] + [f"{names[k]}: {value_text(w.fields[k], self.facts)}"
                                                for k in order if k in w.fields]
            lines += field_lines(w.fields, self.facts, skip=(*order, "day_type"))
            self.emit("schedule", day, ["Shift times", day], w.pages(), "\n".join(lines), facts=[w])

    def speed_limits(self) -> None:
        for sl in self.facts.of("speed_limit"):
            fl = sl.fields
            lines = []
            if "speed_kmh" in fl:
                lines.append(f"Temporary speed limit: {value_text(fl['speed_kmh'], self.facts)}")
            if "road_name" in fl:
                lines.append(f"Road: {value_text(fl['road_name'], self.facts)}")
            if "rp_from" in fl and "rp_to" in fl:
                lines.append(f"Extent: RP {value_text(fl['rp_from'], self.facts)} to RP {value_text(fl['rp_to'], self.facts)}")
            if "length_m" in fl:
                lines.append(f"Length: {value_text(fl['length_m'], self.facts)}")
            for h in (fl["hours_by_day_type"].value if "hours_by_day_type" in fl else []) or []:
                lines.append(f"Hours ({plain(h.get('day_type') or 'all days').replace('_', ' ')}): "
                             f"{h.get('from')}–{h.get('to')} (written {h.get('raw')!r})")
            if "date_from" in fl:
                lines.append(f"Dates: {value_text(fl['date_from'], self.facts)} to "
                             f"{value_text(fl['date_to'], self.facts) if 'date_to' in fl else '?'}")
            tmds = fl["tmd_ids"].value if "tmd_ids" in fl else []
            if tmds:
                lines.append("Diagrams: " + "; ".join(self.sheet_title(t) for t in tmds))
            lines += self.derived_lines(sl)
            raw = next((s.raw for s in sl.sources if s.raw), None)
            if raw:
                lines.append(f"As written: {raw}")
            self.emit("speed_limit", f"{plain(fl['speed_kmh'].value) if 'speed_kmh' in fl else ''}-{sl.fact_id}",
                      ["Temporary speed limits"], sl.pages(), "\n".join(lines), facts=[sl],
                      meta={"tmd_ids": tmds})

    def stages(self) -> None:
        for st in self.facts.of("stage"):
            tid = plain(st.fields["tmd_id"].value) if "tmd_id" in st.fields else "?"
            lines = [f"Stage: {self.sheet_title(tid)}"]
            if "title" in st.fields:
                lines.append(f"Written as: {value_text(st.fields['title'], self.facts)}")
            if "hours" in st.fields:
                lines.append(f"In place: {value_text(st.fields['hours'], self.facts)}")
            recs = {s.record_id for s in st.sources}
            under = [r for r in (self.record(i) for i in sorted(recs)) if r is not None and r.kind == "section"]
            for r in under:
                lines.append(f"Under the heading \"{r.data.get('heading')}\" (page {r.page}): {r.data.get('text')}")
            for m in self.facts.mentions:
                if m.source.record_id in recs and m.mapped_to == tid:
                    note = " — the number does not match the title; the title is used" if m.id_conflict else ""
                    lines.append(f"Listed on page {m.page} as \"{m.text}\"{note}")
            self.emit("stage", tid, ["Work stages", tid], st.pages(), "\n".join(lines),
                      facts=[st, *self._section_facts({r.record_id for r in under})], records=under,
                      meta={"tmd_ids": [tid]})

    def signoffs(self) -> None:
        for so in self.facts.signoffs:
            role = ROLE_NAMES.get(so.role, so.role)
            lines = [f"Sign-off: {role}", f"Status: {so.status.replace('_', ' ')}"]
            for k in ("name", "organisation", "email", "phone", "signature", "date"):
                v = getattr(so, k)
                lines.append(f"{label(k)}: {v if v else 'blank'}")
            lines += [f"Note: {n}" for n in so.notes]
            tables = {s.record_id for s in so.sources}
            rows = [r for r in self.records.of_kind("table_row", page=so.page)
                    if any((t := self.record(tid)) is not None and r.data.get("table") == t.data.get("table")
                           for tid in tables)]
            tbl = [r for r in (self.record(t) for t in tables) if r is not None]
            self.emit("signoff", so.role, ["Sign-off", role], [so.page], "\n".join(lines), records=[*tbl, *rows],
                      elements=[e for s in so.sources for e in s.element_ids])

    def vulnerable_users(self) -> None:
        pairs: dict[str, tuple[str, Record]] = {}
        for t in self.records.of_kind("table"):
            for p in t.data.get("pairs") or []:
                pairs.setdefault(re.sub(r"[^a-z]", "", str(p.get("label") or "").lower()), (str(p.get("value") or ""), t))
        items: list[_Item] = []
        for f in self.facts.of("vulnerable_user"):
            for fname, fv in f.fields.items():
                if fname.endswith("_note"):
                    continue
                written = fv.raw or label(fname)
                state = "ticked" if fv.value is True else "not ticked" if fv.value is False else value_text(fv, self.facts)
                lines = [f"{written}: {state}" + (" (unconfirmed)" if fv.status == Status.UNVERIFIED else "")]
                note = f.fields.get(f"{fname}_note")
                if note is not None and note.value:
                    lines.append(f"Note: {note.value}")
                pv = pairs.get(re.sub(r"[^a-z]", "", written.lower()))
                if pv and pv[0]:
                    lines.append(f"Details: {pv[0]}")
                paths = [f"{f.fact_id}.{fname}"] + ([f"{f.fact_id}.{fname}_note"] if note is not None else [])
                srcs = [s.record_id for s in fv.sources]
                items.append(_Item("\n".join(lines), [s.page for s in fv.sources], paths,
                                   [r for r in (self.record(i) for i in srcs) if r is not None] + ([pv[1]] if pv else [])))
        if items:         # the tick list is one form section: "who are the vulnerable users" meets one chunk
            self.emit_packed("vulnerable_user", "road-users", ["Vulnerable road users"], items)
        for f in self.facts.of("road_aspect"):
            lines = [f"{fv.raw or label(k)}: {'ticked' if fv.value is True else 'not ticked'}"
                     for k, fv in f.fields.items()]
            self.emit("form_fields", "road-aspects", ["Other road aspects"], f.pages(), "\n".join(lines), facts=[f])
        by_page: dict[int, list[Record]] = {}
        for r in self.records.of_kind("checkbox"):        # ticked boxes no catalog entity names
            if r.record_id not in self.used and r.entity is None:
                by_page.setdefault(r.page, []).append(r)
        for page, recs in sorted(by_page.items()):
            lines = []
            for r in recs:
                for k, box in r.fields.items():
                    state = "ticked" if box.value is True else "not ticked" if box.value is False else plain(box.value)
                    lines.append(f"{r.data.get('label') or box.raw or label(k)}: {state}")
            self.emit("form_fields", f"checkboxes-{page}", self.path_for(recs[0]) + ["Checkboxes"], [page],
                      "\n".join(lines), records=recs)

    def yes_no_questions(self) -> None:
        by_page: dict[int, list[Fact]] = {}
        for f in self.facts.of("form_answer"):
            by_page.setdefault(f.pages()[0] if f.pages() else 0, []).append(f)
        auth = self.facts.of("authorisation")
        for a in auth:                                    # a request with no yes/no answers beside it
            for page in a.pages():
                by_page.setdefault(page, [])
        for page, fs in sorted(by_page.items()):
            lines = []
            for f in fs:
                q = f.fields.get("question")
                answers = [fv for k, fv in f.fields.items() if k != "question"]
                lines.append(f"{plain(q.value) if q else '?'} {', '.join(value_text(a, self.facts) for a in answers)}")
            extra = [a for a in auth if page in a.pages()]
            for a in extra:
                if "items" in a.fields:
                    lines.append("Authorisation requested: " + "; ".join(str(i) for i in a.fields["items"].value or []))
            tables = [t for t in self.records.of_kind("table", page=page) if t.record_id not in self.used
                      and any(str(plain(f.fields['question'].value)).lower()[:20] in _text(t).lower()
                              for f in fs if 'question' in f.fields)]
            for t in tables:
                rest = [ln for ln in _text(t).splitlines() if ln.strip() and not any(
                    str(plain(f.fields["question"].value)).lower()[:20] in ln.lower() for f in fs if "question" in f.fields)
                    and not re.fullmatch(r"(Yes|No)?\s*[☐☒☑]?\s*(Yes|No)?\s*[☐☒☑]?", ln.strip())]
                if rest:
                    lines.append("On the form: " + " | ".join(rest))
            first = next((r for r in (self.record(s.record_id) for f in [*fs, *extra] for s in f.sources)
                          if r is not None), None)
            self.emit("yes_no_questions", f"page-{page}", (self.path_for(first) if first else []) or ["Form"],
                      [page], "\n".join(lines), facts=[*fs, *extra], records=tables)

    def form_answers_summary(self) -> None:
        """The yes/no answers and tick lists grouped by answer ("Answered yes (3): …"), so "which questions were
        answered yes?" meets one chunk. Not ticked stays "not ticked": it is not a "no"."""
        answers, ticks = self.facts.of("form_answer"), [*self.facts.of("vulnerable_user"), *self.facts.of("road_aspect")]
        rows: list[tuple[str, dict[str, FactValue]]] = []
        for f in answers:
            q = f.fields.get("question")
            ans = next((fv for k, fv in f.fields.items() if k != "question"), None)
            if q is not None and ans is not None:
                rows.append((plain(q.value), {"answer": ans}))
        lines = _by_value(rows, [("answer", "Answered")], {"yes": 2, "no": 1}, sep="; ")
        for f in ticks:
            name = "Vulnerable road users" if f.entity == "vulnerable_user" else "Road aspects"
            boxes: list[tuple[str, dict[str, FactValue]]] = []
            for fname, fv in f.fields.items():
                if fname.endswith("_note") or not isinstance(fv.value, bool):
                    continue
                note = f.fields.get(f"{fname}_note")
                title = (fv.raw or label(fname)).strip().rstrip(":") + (f" (note: {note.value})" if note is not None
                                                                         and note.value else "")
                boxes.append((title, {"box": FactValue("ticked" if fv.value else "not ticked", status=fv.status)}))
            lines += _by_value(boxes, [("box", name)], {"ticked": 2, "not ticked": 1}, sep="; ")
        if len(lines) < 2:
            return
        facts = [*answers, *ticks]
        self.emit("form_answers_summary", "grouped", ["Form answers, grouped"], sorted({p for f in facts for p in f.pages()}),
                  "Form answers grouped by answer (number of questions or boxes in brackets)\n" + "\n".join(lines),
                  facts=facts)

    def risk_matrix(self) -> None:
        cells = self.facts.of("risk_matrix_cell")
        if not cells:
            return
        lines = ["Risk matrix, one row per likelihood; each cell is impact: score rating"]
        grid: dict[str, list[str]] = {}
        for c in cells:
            fl = c.fields
            lik = value_text(fl["likelihood"], self.facts) if "likelihood" in fl else "?"
            grid.setdefault(lik, []).append(
                f"{value_text(fl['impact'], self.facts) if 'impact' in fl else '?'}: "
                f"{value_text(fl['score'], self.facts) if 'score' in fl else '?'} "
                f"{value_text(fl['rating'], self.facts) if 'rating' in fl else 'no rating'}")
        lines += [f"Likelihood {lik} — " + "; ".join(row)
                  for lik, row in sorted(grid.items(), key=lambda kv: -float(kv[0]) if kv[0].replace(".", "").isdigit() else 0)]
        page = cells[0].pages()[0] if cells[0].pages() else 0
        keys = [r for r in self.records.of_kind("risk_impact_key", "risk_matrix")]
        rows = [r for r in self.records.of_kind("table_row") if r.record_id not in self.used and
                (t := r.data.get("table")) and any(k.data.get("table") == t or r.page == page for k in keys)
                and r.page in {k.page for k in keys}]
        impact_keys = self.records.of_kind("risk_impact_key")
        levels: set[str] = set()
        if impact_keys:
            lines.append("Impact key (impact, rating, WHS outcome, action):")
            for k in impact_keys:
                f = {n: str(fv.raw or plain(fv.value)) for n, fv in k.fields.items()}
                levels.add(f.get("impact", ""))
                lines.append(f"- {f.get('impact', '?')} {f.get('rating', '?')}: {f.get('whs', '')}; {f.get('action', '')}")
        other_rows = [r for r in rows if r.page == page]
        printed = [r for r in other_rows if _text(r).split("\n", 1)[0].strip() not in levels]
        if printed:
            lines.append("Likelihood criteria as printed:")
            lines += [_text(r).replace("\n", " | ") for r in printed]
        grids = [t for t in self.records.of_kind("table") if t.record_id not in self.used and t.data.get("shape") ==
                 "unknown" and any(e in {e2 for c in cells for s in c.sources for e2 in s.element_ids}
                                   for e in t.element_ids())]
        self.emit("risk_matrix", "matrix", ["Risk matrix"], sorted({p for c in cells for p in c.pages()}),
                  "\n".join(lines), facts=cells, records=[*keys, *other_rows, *grids], splittable=False)

    def register_rows(self) -> None:
        for r in self.facts.of("risk"):
            fl = r.fields

            def v(name: str, fl: dict = fl) -> str:
                return value_text(fl[name], self.facts) if name in fl else "not stated"

            title = plain(fl["category"].value) if "category" in fl and fl["category"].value else r.fact_id
            lines = [f"Risk category: {v('category')}", f"Hazard / trigger: {v('hazard')}",
                     f"Groups at risk: {v('groups_at_risk')}", f"Potential event: {v('potential_event')}",
                     f"Initial risk: {v('initial_risk')} (likelihood {v('initial_likelihood')}, consequence "
                     f"{v('initial_consequence')})",
                     f"Control measures: {v('controls')}",
                     f"Residual risk: {v('residual_risk')} (likelihood {v('residual_likelihood')}, consequence "
                     f"{v('residual_consequence')})"]
            for stage in ("initial", "residual"):
                fv = fl.get(f"{stage}_risk")
                lines += [f"Note: {n}" for n in (fv.notes if fv else []) if n.startswith("rating_mismatch")]
            tmds = r.links.get("tmd", [])
            if tmds:
                lines.append("Affected diagram" + ("s" if len(tmds) > 1 else "") + ": " +
                             "; ".join(self.sheet_title(t) for t in tmds))
            known = {"category", "hazard", "groups_at_risk", "potential_event", "initial_risk", "initial_likelihood",
                     "initial_consequence", "controls", "residual_risk", "residual_likelihood", "residual_consequence",
                     "affected_tmds", "initial_check", "residual_check"}
            lines += field_lines({k: x for k, x in fl.items() if k not in known}, self.facts)
            self.emit("register_row", title, ["Risk register", title], r.pages(), "\n".join(lines), facts=[r],
                      meta={"tmd_ids": tmds}, splittable=False)

    def register_summary(self) -> None:
        """Column views of the risk register. A row chunk answers "what are the controls for Stop Go?"; these answer
        "which risks are Very High?", "which went from Very High to Low?" and "list every risk's rating", which a
        top-k search over one chunk per row cuts short. Three sibling chunks: by rating, by likelihood and
        consequence, and one line per risk with every rated column."""
        risks = self.facts.of("risk")
        if len(risks) < 2:
            return
        order: dict[str, float] = {}           # value -> rank: ratings from the impact key, scales from the matrix
        for k in self.records.of_kind("risk_impact_key"):
            if "rating" in k.fields and "impact" in k.fields:
                order[plain(k.fields["rating"].value).lower()] = _num(k.fields["impact"].value)
        rows: list[tuple[str, dict[str, FactValue]]] = []
        mismatches: list[str] = []
        table: list[str] = []
        for r in risks:
            fl = r.fields
            title = plain(fl["category"].value) if "category" in fl and fl["category"].value else r.fact_id
            cols = dict(fl)
            for stage in ("initial", "residual"):
                chk = fl.get(f"{stage}_check")
                cell = chk.value[0] if chk is not None and isinstance(chk.value, list) and chk.value else {}
                for col, num in (("likelihood", "likelihood"), ("consequence", "impact")):
                    fv = fl.get(f"{stage}_{col}")
                    if fv is not None and fv.value and isinstance(cell, dict) and cell.get(num) is not None:
                        order.setdefault(plain(fv.value).lower(), _num(cell[num]))
                pair = _pair(fl.get(f"{stage}_likelihood"), fl.get(f"{stage}_consequence"), " x ", order)
                if pair is not None:
                    cols[f"{stage}_lc"] = pair
                fv = fl.get(f"{stage}_risk")
                mismatches += [f"{title} {stage}: {n.partition(': ')[2]}" for n in (fv.notes if fv else [])
                               if n.startswith("rating_mismatch")]
            move = _pair(fl.get("initial_risk"), fl.get("residual_risk"), " → residual ", order)
            if move is not None:
                cols["move"] = move
            rows.append((title, cols))

            def v(name: str, fl: dict = fl) -> str:
                return value_text(fl[name], self.facts) if name in fl else "not stated"

            tmds = ", ".join(r.links.get("tmd", [])) or "-"
            table.append(f"{title} | {v('initial_risk')} ({v('initial_likelihood')} x {v('initial_consequence')}) | "
                         f"{v('residual_risk')} ({v('residual_likelihood')} x {v('residual_consequence')}) | {tmds} | "
                         f"{pages_text(r.pages())}")
        n = len(rows)
        by_rating = [f"Risk register: {n} risk categories grouped by rating (number of categories in brackets)",
                     *_by_value(rows, [("initial_risk", "Initial risk"), ("residual_risk", "Residual risk"),
                                       ("move", "Initial risk")], order)]
        if mismatches:
            by_rating.append("Rating mismatches (stated rating differs from its risk matrix cell): " + "; ".join(mismatches))
        cells = self.facts.of("risk_matrix_cell")
        grid = [(f"{plain(c.fields['likelihood'].value)} x {plain(c.fields['impact'].value)} = "
                 f"{plain(c.fields['score'].value) if 'score' in c.fields else '?'}", c.fields)
                for c in cells if "likelihood" in c.fields and "impact" in c.fields]
        by_cell = ["Risk matrix cells grouped by rating (number of cells in brackets); each cell is likelihood x impact = score",
                   *_by_value(grid, [("rating", "Cells rated")], order, sep="; ")]
        by_scale = [f"Risk register: {n} risk categories grouped by likelihood and consequence",
                    *_by_value(rows, [("initial_likelihood", "Initial likelihood"),
                                      ("initial_consequence", "Initial consequence"),
                                      ("initial_lc", "Initial likelihood x consequence"),
                                      ("residual_likelihood", "Residual likelihood"),
                                      ("residual_consequence", "Residual consequence"),
                                      ("residual_lc", "Residual likelihood x consequence")], order)]
        pages = sorted({p for r in risks for p in r.pages()})
        path = ["Risk register", "Summary"]
        for key, lines, facts in (("by-rating", by_rating, risks), ("by-likelihood", by_scale, risks)):
            self.emit("register_summary", key, path, pages, "\n".join(lines), facts=facts)
        if len(by_cell) > 1:
            self.emit("register_summary", "matrix-cells", ["Risk matrix", "Summary"], sorted({p for c in cells for p in c.pages()}),
                      "\n".join(by_cell), facts=cells)
        # The one-line-per-risk table is packed into whole lines, each pack with the column header, never overlapping,
        # so a row is never in two chunks; the packs are siblings and come together.
        head = (f"Risk register, one line per risk category ({n} in all): category | initial risk (likelihood x consequence)"
                " | residual risk (likelihood x consequence) | affected diagrams | page")
        packs = size.pack_indices(table, self.k.target_max - tokens.count(head), 0)
        out: list[Chunk] = []
        for i, idx in enumerate(packs):
            part = f" (part {i + 1} of {len(packs)})" if len(packs) > 1 else ""
            out += self.emit("register_summary", "table" if i == 0 else f"table-{i + 1}", path,
                             sorted({p for j in idx for p in risks[j].pages()}),
                             head + part + "\n" + "\n".join(table[j] for j in idx), facts=[risks[j] for j in idx],
                             splittable=False)
        for c in out:
            c.siblings = [o.chunk_id for o in out if o is not c]

    def procedures(self) -> None:
        tables = sorted((r for r in self.records.records.values() if r.kind in ("table", "table_row")
                         and r.record_id not in self.used
                         and self.k.procedure_table.search(_text(r) + " " + " ".join(map(str, r.data.get("headers") or [])))),
                        key=lambda r: (r.page, r.record_id))
        if not tables:
            return
        phase = "installation"
        segments: dict[tuple[str, str], list[str]] = {}
        owners: dict[tuple[str, str], list[Record]] = {}
        for rec in tables:
            cells = sorted(rec.data.get("cells") or [], key=lambda c: (c.get("row", 0), c.get("col", 0)))
            texts = [str(c.get("text") or "").strip() for c in cells] or [_text(rec)]
            for text in texts:
                if not text:
                    continue
                if self.k.procedure_removal.search(text) and len(text) < 40:
                    phase = "removal"
                    continue
                hits = sorted((m.start(), op) for op, rx in self.k.procedure_operations.items() for m in rx.finditer(text))
                bounds = [(0, "general")] + [(s, op) for s, op in hits if s > 0 or op != "general"]
                for i, (start, op) in enumerate(bounds):
                    end = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
                    seg = text[start:end].strip(" -–")
                    if not seg or (op == "general" and start == 0 and hits and hits[0][0] == 0):
                        continue
                    key = ("post_removal", "removal") if op.startswith("post_removal") else (op, phase)
                    segments.setdefault(key, []).append(seg)
                    owners.setdefault(key, [])
                    if rec not in owners[key]:
                        owners[key].append(rec)
            if any(self.k.procedure_removal.search(t) for t in texts):
                phase = "removal"
        for (op, ph), segs in segments.items():
            name = op.replace("_", " ")
            title = f"{name} — {ph}" if op != "general" else f"{ph} (general steps)"
            recs = owners[(op, ph)]
            self.emit("procedure", f"{op}-{ph}", ["Procedures", title], [r.page for r in recs],
                      f"Procedure: {title}\n" + "\n".join(segs), records=recs)
        self.used.update(r.record_id for r in tables)

    def sheets(self) -> None:
        for sh in self.facts.tmd_sheets:
            rec = self.record(sh.record_id)
            if rec is None:
                continue
            d = rec.data
            title = f"{sh.tmd_id} {sh.title.title()}"
            path = ["Diagrams", title]
            syms = d.get("symbols") or []
            counts: dict[str, int] = {}
            for s in syms:
                counts[str(s.get("kind"))] = counts.get(str(s.get("kind")), 0) + (s.get("count") or 1)
            lines = [f"Diagram {sh.tmd_id}: {sh.title.title()} (sheet {sh.sheet}, rev {sh.rev or '?'})"]
            if sh.aliases:
                lines.append("Also referred to as: " + "; ".join(sh.aliases))
            if d.get("roads"):
                lines.append("Roads: " + "; ".join(f"{r.get('name')} ({r.get('level')})" for r in d["roads"]))
            if d.get("legend"):
                lines.append("Legend: " + "; ".join(f"{x.get('swatch')} = {x.get('meaning')}" for x in d["legend"]))
            speeds = sorted({f"{s.get('value')} km/h{' temporary' if s.get('temporary') or s.get('kind') == 'temporary_speed_limit' else ''}"
                             for s in syms if s.get("kind") in ("speed_limit", "temporary_speed_limit") and s.get("value")})
            if speeds:
                lines.append("Speed limits shown: " + ", ".join(speeds))
            rps = sorted({float(s["rp"]) for s in syms if isinstance(s.get("rp"), int | float)})
            if rps:
                lines.append("Route positions: " + ", ".join(f"RP {x:.3f}" for x in rps))
            if d.get("callouts"):
                lines.append("Callouts: " + " | ".join(str(c) for c in d["callouts"]))
            places = list(dict.fromkeys(str(s["value"]).title() for s in syms if s.get("kind") == "place_label" and s.get("value")
                                    and not self.cfg.diagrams.describe.skip_place.search(str(s["value"]))))
            if places:
                lines.append("Places named: " + "; ".join(places))
            houses = list(dict.fromkeys(str(s["value"]) for s in syms if s.get("kind") == "house_label" and s.get("value")))
            if houses:
                lines.append("House numbers shown: " + ", ".join(houses))
            lines.append("Drawn items: " + ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(counts.items())))
            lines.append("More on this diagram: all its text, positions and order of items, every distance and what its "
                         "line runs between, the title block, and a description of the image.")
            meta = {"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id], "rp_values": rps}
            overview = self.emit("sheet_overview", sh.tmd_id, path, [sh.page], "\n".join(lines), records=[rec],
                                 meta=meta, splittable=False)[0]
            self._sheet_figures(sh, rec, overview)
            self._sheet_image_facts(sh, rec, overview, path)
            ordered = [s for s in syms if s.get("order") is not None]
            approaches: dict[str, list[dict]] = {}
            for s in ordered:
                approaches.setdefault(str(s.get("approach") or "all"), []).append(s)
            for appr, signs in approaches.items():
                signs.sort(key=lambda s: s.get("order") or 0)
                for i in range(0, len(signs), self.k.sign_schedule_max):
                    batch = signs[i:i + self.k.sign_schedule_max]
                    rule = next((s.get("extra", {}).get("direction_of_travel") for s in batch
                                 if isinstance(s.get("extra"), dict) and s["extra"].get("direction_of_travel")), None)
                    lines = [f"Signs on {appr}, numbered along the road" +
                             (f" (direction of travel: {rule})" if rule else "") + ":"]
                    for s in batch:
                        what = " ".join(str(x) for x in (s.get("value"), s.get("unit")) if x not in (None, ""))
                        txt = " ".join(str(s.get("text") or "").split())
                        lines.append(f"{s.get('order')}. {str(s.get('kind')).replace('_', ' ')}"
                                     + (f" {what}" if what else "") + (f" \"{txt}\"" if txt and txt != what else "")
                                     + (" (temporary)" if s.get("temporary") else "")
                                     + (f" at RP {float(s['rp']):.3f}" if isinstance(s.get("rp"), int | float) else ""))
                    self.emit("sheet_sign_schedule", f"{sh.tmd_id}-{appr}-{i // self.k.sign_schedule_max}",
                              path + ["Sign schedule"], [sh.page], "\n".join(lines), records=[rec],
                              meta={"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id],
                                    "rp_values": sorted({float(s["rp"]) for s in batch
                                                         if isinstance(s.get("rp"), int | float)})},
                              parent=overview.chunk_id, splittable=False)
            if d.get("notes"):
                self.emit("sheet_notes", sh.tmd_id, path + ["Notes"], [sh.page],
                          "Notes:\n" + "\n".join(str(n) for n in d["notes"]), records=[rec],
                          meta={"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id]}, parent=overview.chunk_id)

    def _sheet_image_facts(self, sh, rec: Record, overview: Chunk, path: list[str]) -> None:
        """What the image of a sheet shows, for image questions: text, layout, distances, details and description.
        Code facts (``diagrams.describe``) are stated plainly; model statements (X13, X8) carry "(unconfirmed)" unless
        they passed their checks."""
        d = rec.data
        facts = (d.get("describe") or {}).get("facts")
        if not facts:
            return
        read = (d.get("describe") or {}).get("read") or {}
        on = f"diagram {sh.tmd_id} {sh.title.title()} (page {sh.page})"
        meta = {"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id]}
        items = {i["id"]: i for i in facts["items"]}
        seen = {x["item_id"]: x for x in read.get("items") or [] if x.get("item_id") != "new"}
        readings = {g["printed"]: g for g in read.get("garbled") or []}

        def mark(x: dict) -> str:
            return "" if x.get("ok") else " (unconfirmed)"

        def shown(text: str) -> str:
            g = readings.get(text)
            if g and g.get("reads_as"):
                return f'"{g["reads_as"]}" (printed in a broken font as "{text}"){mark(g)}'
            return f'"{text}" (printed in a broken font; not read)' if self.cfg.diagrams.describe.garbled.search(text) else f'"{text}"'

        def emit(ctype: str, lines: list[str]) -> None:
            if len(lines) > 1:
                self.emit(ctype, sh.tmd_id, path + [ctype.replace("sheet_", "").title()], [sh.page], "\n".join(lines),
                          records=[rec], meta=meta, parent=overview.chunk_id)

        def look(iid: str) -> str:
            x = seen.get(iid)
            if not x or items.get(iid, {}).get("kind") in _PLAIN_LABELS:
                return ""
            bits = [f"looks like {x['looks_like']}" if x.get("looks_like") else "",
                    f"meaning: {x['meaning']}" if x.get("meaning") and x.get("meaning") != "unclear" else "",
                    f"side of road: {x['road_side']}" if x.get("road_side") and x.get("road_side") != "unclear" else ""]
            bits = [b for b in bits if b]
            return f"; {'; '.join(bits)}{mark(x)}" if bits else ""

        # all text, as printed, grouped by where it is
        lines = [f"Text printed on {on}, as printed, with where it is:"]
        for t in facts["texts"]:
            lines.append(f"- [{t['where']}, {t['location']}] {shown(t['text'])}")
        emit("sheet_text", lines)

        # positions and order
        lines = [f"Layout of {on}. Positions are on the plan: top/middle/bottom and left/centre/right.",
                 "From left to right across the plan:"]
        for n, iid in enumerate(facts["left_to_right"], 1):
            i = items[iid]
            road = f"; on {i['road']}" if i.get("road") else ""
            rp = f"; RP {float(i['rp']):.3f}" if isinstance(i.get("rp"), int | float) else ""
            lines.append(f"{n}) {i['label']} ({i['location']}{road}{rp}){look(iid)}")
        for where, head in (("inset", "Inside the inset (a separate close-up view):"),
                            ("figure", "Inside the pictures beside the plan:")):
            group = [i for i in facts["items"] if i["where"] == where]
            if group:
                lines.append(head)
                lines += [f"- {i['label']} ({i['location']}){look(i['id'])}" for i in group]
        for kind, head in (("callout", "Notices on the plan"), ("road_label", "Road names written on the plan"),
                           ("place_label", "Places named on the plan")):
            group = [i for i in facts["items"] if i["kind"] == kind and i["where"] == "plan"]
            if group:
                lines.append(f"{head}: " + "; ".join(f"{i['label']} ({i['location']})" for i in group))
        lines += [f"Seen in the image ({x.get('where') or 'position not stated'}): {x.get('looks_like')}{mark(x)}"
                  for x in read.get("items") or [] if x.get("item_id") == "new" and x.get("looks_like")]
        for r in read.get("relations") or []:
            a, b = items.get(str(r.get("a"))), items.get(str(r.get("b")))
            if a and b and r.get("relation"):
                lines.append(f"{a['label']} is {r['relation']} {b['label']}{mark(r)}")
        emit("sheet_layout", lines)

        # distances
        measures = {str(x.get("item_id")): x for x in read.get("distances") or []}
        lines = [f"Distances drawn on {on}, in order along the drawing (left to right, then the upright ones), with "
                 "what each dimension line runs between (from the drawing's geometry):"]
        for x in facts["distances"]:
            v = f"{x['value']:g} {x['unit']}" if isinstance(x.get("value"), int | float) else "a distance (value unreadable)"
            ends = (f"runs from {x['start'] or 'a point with nothing drawn at it'} to {x['end'] or 'a point with nothing drawn at it'}"
                    if x.get("start") or x.get("end") else "its ends do not meet a drawn item")
            nxt = [f"follows straight on from the {items[x['before']]['label'].replace('distance label ', '')} line"
                   if x.get("before") in items else "",
                   f"continues into the {items[x['after']]['label'].replace('distance label ', '')} line"
                   if x.get("after") in items else ""]
            m = measures.get(x["id"])
            what = f"; measures: {m['measures']}{mark(m)}" if m and m.get("measures") else ""
            lines.append(f"- {v} ({x['location']}): the line {ends}"
                         + "".join(f"; {b}" for b in nxt if b) + what)
        emit("sheet_distances", lines)

        # title block and legend
        tb = [t for t in facts["texts"] if t["where"] == "title block"]
        lines = [f"Title block and legend of {on}:"]
        lines += [f"- {shown(t['text'])}" for t in tb]
        if d.get("legend"):
            lines.append("Legend: " + "; ".join(f"{x.get('swatch')} = {x.get('meaning')}" for x in d["legend"]))
        emit("sheet_details", lines)

        # what the image shows (model)
        texts = read.get("texts") or {}
        lines = [f"What the image of {on} shows, as read by a model (statements that failed a check are marked "
                 "unconfirmed):"]
        for key, head in (("description", "Description"), ("drawing_type", "Type of drawing"), ("purpose", "Purpose"),
                          ("pedestrians", "Pedestrians"), ("vehicles", "Vehicles")):
            if key in texts:
                lines.append(f"{head}: {texts[key]['text']}{mark(texts[key])}")
        lines += _sheet_summary(d.get("summary"))
        lines += [f"Could not be read: {u.get('what')} ({u.get('where')})" for u in read.get("unreadable") or []]
        lines += [f"Inferred, not read from the image: {x}" for x in read.get("inferred") or []]
        if len(lines) == 1:
            lines.append("(No model has read this image yet: only the text, layout, distances and title block are known.)")
        emit("sheet_image", lines)

    def _sheet_figures(self, sh, rec: Record, overview: Chunk) -> None:
        """The pictures drawn beside a diagram (pedestrian options, notices) as figure chunks tied to its TMD."""
        seen: set[str] = set()
        for n, f in enumerate(rec.data.get("figures") or []):
            desc = str(f.get("description") or "").strip()
            kind = str(f.get("kind") or "figure")
            key = f"{sh.tmd_id}-{n}-{slugify(desc[:40])}"
            if not desc or "logo" in kind.lower() or desc in seen:
                continue
            seen.add(desc)
            title = str(f.get("title") or "").strip()
            lines = [f"Image: {kind}", f"Shown on {pages_text([sh.page])}, beside diagram {sh.tmd_id}"]
            if title.isascii() and title:
                lines.append(f"Title: {title}")
            lines.append(f"Description: {desc}" + ("" if f.get("verified") else " (unconfirmed)"))
            lines += _full_lines(f.get("full"))
            self.emit("figure_caption", key, ["Diagrams", f"{sh.tmd_id} {sh.title.title()}", "Figure"], [sh.page], "\n".join(lines),
                      records=[rec], meta={"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id]},
                      parent=overview.chunk_id, splittable=False)

    def figures(self) -> None:
        groups: dict[str, list[Fact]] = {}
        for f in self.facts.of("figure"):
            rec = next((self.record(s.record_id) for s in f.sources if self.record(s.record_id)), None)
            key = str(rec.data.get("phash") or rec.data.get("reused_from") if rec else f.fact_id)
            groups.setdefault(key, []).append(f)
        for key, fs in groups.items():
            recs = [r for r in (self.record(s.record_id) for f in fs for s in f.sources) if r is not None]
            main = recs[0] if recs else None
            kind = next((plain(f.fields["image_kind"].value) for f in fs
                         if "image_kind" in f.fields and f.fields["image_kind"].value), "figure")
            lines = [f"Image: {kind}", f"Shown on {pages_text([p for f in fs for p in f.pages()])}"]
            labels = (main.data.get("labels") or []) if main else []
            if labels:
                lines.append("Labels: " + "; ".join(f"\"{x.get('text')}\"" for x in labels if x.get("text")))
            numbers = (main.data.get("numbers") or []) if main else []
            if numbers:
                lines.append("Numbers: " + "; ".join(f"{n.get('value')} {n.get('unit') or ''} {n.get('meaning') or ''}".strip()
                                                    for n in numbers))
            desc = next((f.fields["description"] for f in fs if "description" in f.fields
                         and f.fields["description"].value), None)
            lines.append(f"Description: {value_text(desc, self.facts)}" if desc else
                         "Description: none yet (the image has not been read by a model)")
            lines += _full_lines(main.data.get("full") if main else None)
            self.emit("figure_caption", key[:16], self.path_for(main) + ["Figure"] if main else ["Figure"],
                      [p for f in fs for p in f.pages()], "\n".join(lines), facts=fs, records=recs,
                      meta={"tmd_ids": sorted({t for f in fs for t in f.links.get("tmd", [])})})

    def standard_rules(self) -> None:
        pages = {p for p, z in self.ctx.zones.items() if z == "generic_standard"}
        for rec in sorted(self.records.records.values(), key=lambda r: (r.page, r.record_id)):
            if rec.page not in pages or rec.record_id in self.used or rec.applicability == "not_applicable":
                continue
            if rec.kind == "section":
                text = f"{rec.data.get('heading', '')}\n{rec.data.get('text', '')}".strip()
                facts = self._section_facts({rec.record_id})
            else:
                text, facts = _text(rec), []
            if not text.strip():
                continue
            name = str(rec.data.get("heading") or rec.record_id)
            self.emit("standard_rule", f"{rec.page}-{name}", self.path_for(rec) or ["Standard"], [rec.page], text,
                      facts=facts, records=[rec], meta={"scope": "generic"})

    def narrative_sections(self) -> None:
        secs = [r for r in sorted(self.records.of_kind("section"), key=lambda r: (r.page, self._order(r)))
                if r.record_id not in self.used and r.applicability != "not_applicable"
                and (r.data.get("text") or "").strip()]
        groups: list[tuple[str, list[Record]]] = []
        for r in secs:
            top = str((r.data.get("path") or [r.data.get("heading") or r.record_id])[0])
            if groups and groups[-1][0] == top and r.page - groups[-1][1][-1].page <= 1:
                groups[-1][1].append(r)
            else:
                groups.append((top, [r]))
        for top, recs in groups:
            units: list[tuple[list[Record], str]] = []
            for r in recs:
                level = int(r.data.get("level") or 1)
                heading = str(r.data.get("heading") or "").strip()
                body = f"{heading}\n{r.data.get('text', '')}".strip() if (level > 1 or heading != top) else str(r.data.get("text") or "")
                units.append(([r], body))
            merged: list[tuple[list[Record], str]] = []
            for rs, body in units:                     # pack whole sections up to the target size
                if merged and tokens.count(merged[-1][1] + "\n\n" + body) <= self.k.target_max:
                    merged[-1] = (merged[-1][0] + rs, merged[-1][1] + "\n\n" + body)
                else:
                    merged.append((rs, body))
            packs: list[Chunk] = []
            for rs, body in merged:
                path = [top] + ([str(rs[0].data.get("heading"))] if len(rs) == 1 and rs[0].data.get("level", 1) > 1 else [])
                packs += self.emit("narrative_section", top, path, [p for r in rs for p in r.data.get("pages", [r.page])],
                                   body, facts=self._section_facts({r.record_id for r in rs}), records=rs)
            self._number_parts(packs, "narrative_section", top)

    def _number_parts(self, chunks: list[Chunk], ctype: str, key: str) -> None:
        """One section that became several chunks (whole sections packed, or one section split): number them
        ``:part-1 .. :part-n`` with "(part k of n)" in the header and link them as siblings, so a hit on one part
        brings the adjacent parts (``retrieval.search._neighbours``). Ids stay unique, however the parts arose."""
        if len(chunks) < 2:
            return
        for c in chunks:
            self._ids.discard(c.chunk_id.rsplit(":part-", 1)[0] if ":part-" in c.chunk_id else c.chunk_id)
        base, n = f"{self.ctx.slug}:{ctype}:{slugify(key)}", 2
        while any(i == base or i.startswith(f"{base}:part-") for i in self._ids):
            base, n = f"{self.ctx.slug}:{ctype}:{slugify(key)}-{n}", n + 1
        self._ids.add(base)
        ids = [f"{base}:part-{k}" for k in range(1, len(chunks) + 1)]
        for k, (c, cid) in enumerate(zip(chunks, ids), 1):
            c.chunk_id, c.siblings = cid, [o for o in ids if o != cid]
            c.header = header(self.ctx, self.k.breadcrumb_sep, c.metadata["pages"], c.metadata["section_path"],
                              f" (part {k} of {len(chunks)})")
            c.metadata["part"] = f"{k} of {len(chunks)}"

    def _order(self, rec: Record) -> float:
        return -self._top(rec)

    @staticmethod
    def _groups_not_written(rec: Record, corpus: Corpus) -> list[tuple[str | None, str]]:
        """The groups of a label/value table that another builder used, keeping each group that has a value no chunk
        holds yet, whole ("Meeting location" and "Apologies" beside the contacts the stakeholder chunk already
        gives); a group the entity chunks already say in full is left out."""
        pairs = rec.data.get("pairs") or []
        missing = {p.get("group") for p in pairs
                   if not corpus.has_pair(str(p.get("label") or ""), str(p.get("value") or ""))}
        keep = [p for p in pairs if p.get("group") in missing]
        if not keep:
            return []
        return _table_rows(Record(rec.kind, rec.page, rec.produced_by, {}, rec.entity, data={**rec.data, "pairs": keep}))

    def form_fields(self) -> None:
        """Tables as rows (a row is never split), one chunk per table group (DAY SHIFT (during School days) ...). A
        table another builder used is still written here unless all its values already are in some chunk."""
        corpus = Corpus([c.content for c in self.chunks])
        for rec in sorted(self.records.of_kind("table"), key=lambda r: (r.page, r.record_id)):
            if rec.applicability == "not_applicable":
                continue
            if rec.record_id in self.used:        # an entity builder rendered it: add only what it left out
                if not rec.data.get("pairs"):
                    continue
                groups = _grouped(self._groups_not_written(rec, corpus))
            else:
                groups = _grouped(_table_rows(rec))
            if not groups:
                continue
            cells = sorted(rec.data.get("cells") or [], key=lambda c: (c.get("row", 0), c.get("col", 0)))
            first = (str(cells[0].get("text") or "").strip()[:60] if cells else rec.record_id)
            facts = [f for f in self.facts.facts if f.entity == "road_geometry"
                     and any(s.record_id == rec.record_id or set(s.element_ids) & set(rec.element_ids())
                             for s in f.sources)]
            group_elements: dict[str | None, list[str]] = {}       # the cells of a group's rows (and its heading)
            for p in rec.data.get("pairs") or []:
                group_elements.setdefault(p.get("group"), []).extend(p.get("elements") or [])
            for n, (group, lines) in enumerate(groups):
                title = group.rstrip(":") if group else (first if n == 0 else f"{first} ({n + 1})")
                head = [f"{title}:"] if group else []
                lead = ("\n".join(field_lines(facts[0].fields, self.facts)) + "\n\nAs written:") if facts and n == 0 else ""
                budget = self.k.target_max - tokens.count("\n".join([lead, *head]))
                # a row is whole unless it alone is over the budget (a paragraph in one cell): then it splits at sentences
                lines = [part for ln in lines for part in (size.split(ln, budget, 0) if tokens.count(ln) > budget else [ln])]
                parts = size.pack_indices(lines, budget, 0)
                out: list[Chunk] = []
                for k, idx in enumerate(parts):
                    body = "\n".join([*([lead] if lead and k == 0 else []), *head, *(lines[i] for i in idx)])
                    out += self.emit("form_fields", f"{rec.page}-{title}" + (f"-{k + 1}" if k else ""),
                                     self.path_for(rec) + [title], [rec.page], body,
                                     facts=facts if k == 0 and n == 0 else (), records=[rec], splittable=False,
                                     meta={"table_group": title} if group else None)
                if len(out) > 1:
                    for c in out:
                        c.siblings = [o.chunk_id for o in out if o is not c]
                if len(groups) > 1 and group_elements.get(group):
                    for c in out:             # a group chunk is about its own rows, not the whole table
                        c.element_ids = list(dict.fromkeys(group_elements[group]))
        for f in self.facts.of("road_geometry"):
            if not any(f"{f.fact_id}.{k}" in c.field_paths for c in self.chunks for k in f.fields):
                self.emit("form_fields", f"road-geometry-{f.fact_id}", ["Road geometry"], f.pages(),
                          "\n".join(field_lines(f.fields, self.facts)), facts=[f])
        for f in self.facts.facts:            # catalog entities no other builder renders (one chunk per fact)
            if f.entity in ("work_period", "tmp_document") or f.entity.startswith(("contingency", "monitoring")):
                if any(f"{f.fact_id}.{k}" in c.field_paths for c in self.chunks for k in f.fields if c.chunk_type != "doc_card"):
                    continue
                name = {"work_period": "Operating hours", "tmp_document": "Document"}.get(f.entity, f.entity)
                # the TMP reference is printed in every page header, but the document's identity (reference, order
                # reference, approval) is stated once, on page 1: a small chunk that answers identity questions precisely
                pages = [1] if f.entity == "tmp_document" else f.pages() or [1]
                self.emit("form_fields", f"{f.entity}-{f.fact_id}", [name], pages,
                          "\n".join(field_lines(f.fields, self.facts)), facts=[f])

    def narrative_table_rows(self) -> None:
        for rec in sorted(self.records.of_kind("table_row"), key=lambda r: (r.page, r.record_id)):
            if rec.record_id in self.used:
                continue
            headers = [str(h or "").strip() for h in rec.data.get("headers") or []]
            cells = sorted(rec.data.get("cells") or [], key=lambda c: c.get("col", 0))
            lines = []
            for c in cells:
                t = str(c.get("text") or "").strip()
                if not t:
                    continue
                col = int(c.get("col", 0))
                h = headers[col] if col < len(headers) and headers[col] else ""
                lines.append(f"{h}: {t}" if h else t)
            if not lines:
                continue
            title = re.sub(r"\s+", " ", lines[0])[:50]
            self.emit("narrative_table_row", f"{rec.page}-{title}", self.path_for(rec) + [title], [rec.page],
                      "\n".join(lines), records=[rec])

    def other_sections(self) -> None:
        """Leftover elements and any record with text that no builder rendered, with 10-15% overlap."""
        texts: list[tuple[int, str, list[str], list[Record]]] = []
        for e in self.store.elements.values():
            if e.outcome == Outcome.LEFTOVER and str(e.representations.get("text") or "").strip():
                texts.append((e.page, str(e.representations["text"]), [e.element_id], []))
        for rec in self.records.records.values():
            if rec.record_id in self.used or rec.applicability == "not_applicable" or rec.kind == "diagram_sheet":
                continue
            t = _text(rec) if rec.kind != "section" else f"{rec.data.get('heading', '')}\n{rec.data.get('text', '')}"
            if t.strip():
                texts.append((rec.page, t, rec.element_ids(), [rec]))
        if not texts:
            return
        units: list[str] = []
        origin: list[int] = []                       # for each unit, the index in ``texts`` it came from
        for n, (_, t, _, _) in enumerate(texts):
            for u in size.pieces(t):
                units.append(u)
                origin.append(n)
        overlap = max(1, round(len(units) * self.k.other_overlap)) if len(units) > 4 else 0
        for i, part in enumerate(size.pack_indices(units, self.k.target_max, overlap)):
            src = [texts[n] for n in dict.fromkeys(origin[u] for u in part)]      # only what this part is made of
            self.emit("other_section", f"other-{i}", ["Other content"], [p for p, *_ in src],
                      "\n".join(units[u] for u in part),
                      records=[r for *_, rs in src for r in rs], elements=[e for _, _, es, _ in src for e in es],
                      splittable=False)
