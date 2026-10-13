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

from ..config import Config
from ..facts.model import Fact, Facts, Source
from ..models import ElementStore
from ..records import Record, RecordStore, Status
from ..vocab import Outcome
from . import size, tokens
from .context import DocContext, slugify
from .model import Chunk
from .text import field_lines, header, label, pages_text, plain, value_text

ROLE_NAMES = {"ttm_designer": "TTM designer", "design_checker": "Design checker", "activity_manager": "Activity manager",
              "rca_primary": "RCA review, primary network", "rca_other": "RCA review, other network",
              "rca_authorisation_form3": "RCA authorisation (Form 3)"}


def _text(rec: Record) -> str:
    """The text a table-like record holds: its pairs, else its cells in reading order, else its field values."""
    d = rec.data
    if d.get("pairs"):
        return "\n".join(f"{p.get('label') or ''}: {p.get('value') or ''}".strip(": ") for p in d["pairs"])
    cells = sorted(d.get("cells") or [], key=lambda c: (c.get("row", 0), c.get("col", 0)))
    if cells:
        return "\n".join(str(c.get("text") or "").strip() for c in cells if str(c.get("text") or "").strip())
    return "\n".join(f"{label(k)}: {fv.raw if fv.raw is not None else plain(fv.value)}"
                     for k, fv in rec.fields.items() if fv.value is not None or fv.raw)


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
        pages = sorted(set(pages) or {s.page for s in sources} or {r.page for r in records})
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
        for i, part in enumerate(parts, 1):
            cid = base if len(parts) == 1 else f"{base}:part-{i}"
            label_ = "" if len(parts) == 1 else f" (part {i} of {len(parts)})"
            c = Chunk(cid, ctype, header(self.ctx, self.k.breadcrumb_sep, pages, path, label_), part.strip(),
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

    def record(self, rid: str) -> Record | None:
        return self.records.records.get(rid)

    def sheet_title(self, tmd_id: str) -> str:
        sh = self.facts.sheet(tmd_id)
        return f"{tmd_id} {sh.title.title()}" if sh else tmd_id

    # --- builders ------------------------------------------------------------------------------------------------
    def build(self) -> list[Chunk]:
        for step in (self.doc_card, self.stakeholders, self.activity, self.roads, self.schedules, self.speed_limits,
                     self.stages, self.signoffs, self.vulnerable_users, self.yes_no_questions, self.risk_matrix,
                     self.register_rows, self.procedures, self.sheets, self.figures, self.standard_rules,
                     self.narrative_sections, self.form_fields, self.narrative_table_rows, self.other_sections):
            step()
        return self.chunks

    def doc_card(self) -> None:
        c, f = self.ctx, self.facts
        lines = [f"TMP reference: {c.tmp_ref}" + (f" (also written {', '.join(c.tmp_ref_aliases)})"
                                                     if c.tmp_ref_aliases else "")]
        doc = next(iter(f.of("tmp_document")), None)
        if doc and "order_reference" in doc.fields:
            lines.append(f"Order reference: {value_text(doc.fields['order_reference'], f)}")
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

    def roads(self) -> None:
        for r in self.facts.of("road"):
            name = plain(r.fields["road_name"].value) if "road_name" in r.fields else r.fact_id
            self.emit("road", name, ["Roads", name], r.pages(), "\n".join(field_lines(r.fields, self.facts)), facts=[r],
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
        tables: set[str] = set()
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
                    tables.add(pv[1].record_id)
                paths = [f"{f.fact_id}.{fname}"] + ([f"{f.fact_id}.{fname}_note"] if note is not None else [])
                srcs = [s.record_id for s in fv.sources]
                self.emit("vulnerable_user", fname, ["Vulnerable road users", written], [s.page for s in fv.sources],
                          "\n".join(lines), paths=paths,
                          records=[r for r in (self.record(i) for i in srcs) if r is not None] +
                          ([pv[1]] if pv else []))
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
        parent = f"{self.ctx.slug}:procedure"
        for (op, ph), segs in segments.items():
            name = op.replace("_", " ")
            title = f"{name} — {ph}" if op != "general" else f"{ph} (general steps)"
            recs = owners[(op, ph)]
            self.emit("procedure", f"{op}-{ph}", ["Procedures", title], [r.page for r in recs],
                      f"Procedure: {title}\n" + "\n".join(segs), records=recs, parent=parent)
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
            lines.append("Drawn items: " + ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(counts.items())))
            if isinstance(d.get("summary"), dict) and d["summary"].get("text"):
                lines.append(f"Summary: {d['summary']['text']}")
            elif isinstance(d.get("summary"), str) and d["summary"]:
                lines.append(f"Summary: {d['summary']}")
            for sid, cap in (d.get("captions") or {}).items():
                if isinstance(cap, dict) and cap.get("caption"):
                    lines.append(f"Symbol {sid}: {cap['caption']}" + ("" if cap.get("status") == "verified" else " (unconfirmed)"))
            if not d.get("summary"):
                lines.append("(No spatial summary yet: the sheet has not been read by a model.)")
            meta = {"tmd_ids": [sh.tmd_id], "linked_sheets": [sh.tmd_id], "rp_values": rps,
                    "symbols": [{k: s.get(k) for k in ("id", "kind", "value", "rp", "road", "bbox")} for s in syms]}
            overview = self.emit("sheet_overview", sh.tmd_id, path, [sh.page], "\n".join(lines), records=[rec],
                                 meta=meta, splittable=False)[0]
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

    def figures(self) -> None:
        groups: dict[str, list[Fact]] = {}
        for f in self.facts.of("figure"):
            rec = next((self.record(s.record_id) for s in f.sources if self.record(s.record_id)), None)
            key = str((rec.data.get("reused_from") or rec.data.get("phash")) if rec else f.fact_id)
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
            self.emit("figure_caption", key[:16], self.path_for(main) + ["Figure"] if main else ["Figure"],
                      [p for f in fs for p in f.pages()], "\n".join(lines), facts=fs, records=recs,
                      meta={"tmd_ids": sorted({t for f in fs for t in f.links.get("tmd", [])})}, splittable=False)

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
            for i, (rs, body) in enumerate(merged):
                key = f"{top}-{i}" if len(merged) > 1 else top
                path = [top] + ([str(rs[0].data.get("heading"))] if len(rs) == 1 and rs[0].data.get("level", 1) > 1 else [])
                part_meta = {"part": f"{i + 1} of {len(merged)}"} if len(merged) > 1 else {}
                chunks = self.emit("narrative_section", key, path, [p for r in rs for p in r.data.get("pages", [r.page])],
                                   body, facts=self._section_facts({r.record_id for r in rs}), records=rs, meta=part_meta)
                if len(merged) > 1:
                    for c in chunks:
                        c.parent_id = f"{self.ctx.slug}:narrative_section:{slugify(top)}"

    def _order(self, rec: Record) -> float:
        return -self._top(rec)

    def form_fields(self) -> None:
        for rec in sorted(self.records.of_kind("table"), key=lambda r: (r.page, r.record_id)):
            if rec.record_id in self.used or rec.applicability == "not_applicable":
                continue
            text = _text(rec)
            if not text.strip():
                continue
            cells = sorted(rec.data.get("cells") or [], key=lambda c: (c.get("row", 0), c.get("col", 0)))
            title = next((p.get("group") for p in rec.data.get("pairs") or [] if p.get("group")), None) or \
                (str(cells[0].get("text") or "").strip()[:60] if cells else rec.record_id)
            facts = [f for f in self.facts.facts if f.entity not in ("section",)
                     and any(s.record_id == rec.record_id or set(s.element_ids) & set(rec.element_ids())
                             for s in f.sources) and f.entity == "road_geometry"]
            if facts:
                text = "\n".join(field_lines(facts[0].fields, self.facts)) + "\n\nAs written:\n" + text
            self.emit("form_fields", f"{rec.page}-{title}", self.path_for(rec) + [title], [rec.page], text,
                      facts=facts, records=[rec])
        for f in self.facts.of("road_geometry"):
            if not any(f"{f.fact_id}.{k}" in c.field_paths for c in self.chunks for k in f.fields):
                self.emit("form_fields", f"road-geometry-{f.fact_id}", ["Road geometry"], f.pages(),
                          "\n".join(field_lines(f.fields, self.facts)), facts=[f])
        for f in self.facts.facts:            # catalog entities no other builder renders (one chunk per fact)
            if f.entity in ("work_period", "tmp_document") or f.entity.startswith(("contingency", "monitoring")):
                if any(f"{f.fact_id}.{k}" in c.field_paths for c in self.chunks for k in f.fields if c.chunk_type != "doc_card"):
                    continue
                name = {"work_period": "Operating hours", "tmp_document": "Document"}.get(f.entity, f.entity)
                self.emit("form_fields", f"{f.entity}-{f.fact_id}", [name], f.pages() or [1],
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
        body = [t for _, t, _, _ in texts]
        units = [u for t in body for u in size.pieces(t)]
        overlap = max(1, round(len(units) * self.k.other_overlap)) if len(units) > 4 else 0
        parts = size.pack(units, self.k.target_max, overlap)
        for i, part in enumerate(parts):
            self.emit("other_section", f"other-{i}", ["Other content"], [p for p, *_ in texts], part,
                      records=[r for *_, rs in texts for r in rs], elements=[e for _, _, es, _ in texts for e in es],
                      splittable=False)
