"""Text handler (plan section 2.1): free text becomes sections with headings, paragraphs and facts.

Everything here is code except the two model hooks the plan allows: X4 names a section whose heading the
catalog does not know, and X5 reads facts for section types that have catalog fields (X6 then judges critical
ones). The model never edits text: text is stored exactly as read, with only rule-based fixes (a word split
by a line-end hyphen is joined). Without a model, unknown headings stay unnamed and X5-only fields are simply
absent; critical facts that nothing judged are stored ``unverified``.

Typography decides headings: body size is the most common size; a bold line at least ``heading_min_delta`` pt
larger is level 1; a bold line at body size that stands alone is level 2; a run of two or more bold body
lines is a paragraph (stacked bold lines inside a rule box). Rotated side labels are level 3.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .. import logging_ as lg
from ..catalog import FieldDef
from ..contracts import BaseHandler, ElementUpdate, HandlerContext, HandlerResult
from ..model import prompts
from ..model.client import ask
from ..model.validators import check_fact, check_x4, check_x6
from ..models import Element
from ..numerics import Numeric, extract
from ..records import Evidence, FieldValue, Record, Status
from ..vocab import Flag, Route
from .textutil import LIST_ITEM, join_lines


@dataclass
class _Line:
    element: Element
    text: str
    size: float
    bold: bool
    struck: bool
    rotated: bool


@dataclass
class _Section:
    heading: str | None
    level: int
    path: list[str]
    heading_lines: list[_Line] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)       # body lines, in reading order, with their blocks


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence (or line) of ``text`` that holds the span, as the quote for a pattern fact."""
    left = max([text.rfind(".", 0, start), text.rfind("\n", 0, start), text.rfind("!", 0, start),
                text.rfind("?", 0, start)])
    cut = [i for i in (text.find(c, end) for c in ".\n!?") if i != -1]
    right = min(cut) + 1 if cut else len(text)
    return text[left + 1:right].strip()


class TextHandler(BaseHandler):
    route = Route.TEXT
    version = "1.0.0"

    def __init__(self) -> None:
        self._blocks: list[Element] = []

    def begin(self, ctx: HandlerContext) -> None:
        self._blocks = []

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        self._blocks.append(element)             # sections span blocks and pages: structure is built in finish()
        return HandlerResult()

    # --- structure -----------------------------------------------------------------------------------------------

    def _lines(self) -> list[_Line]:
        out = []
        for b in self._blocks:
            rotated = b.metadata.get("rotation", 0) not in (0, 180)
            for ln in b.representations.get("lines", []):
                if ln["text"].strip():
                    out.append(_Line(b, ln["text"].strip(), ln["size"], ln["bold"], ln.get("struck", False), rotated))
        return out

    def _body_size(self, lines: list[_Line]) -> float:
        weight: Counter[float] = Counter()
        for ln in lines:
            weight[round(ln.size * 2) / 2] += len(ln.text)
        return weight.most_common(1)[0][0] if weight else 0.0

    def _levels(self, lines: list[_Line], body: float, min_delta: float) -> list[int]:
        """Heading level per line (0 = body text)."""
        levels = [0] * len(lines)
        for i, ln in enumerate(lines):
            if ln.rotated:
                levels[i] = 3
            elif ln.bold and ln.size >= body + min_delta:
                levels[i] = 1
            elif ln.bold and abs(ln.size - body) < min_delta:
                levels[i] = 2
        i = 0
        while i < len(lines):                    # a run of bold body lines in one block is a paragraph, not headings
            if levels[i] == 2:
                j = i
                while j + 1 < len(lines) and levels[j + 1] == 2 and lines[j + 1].element is lines[i].element:
                    j += 1
                if j > i:
                    for k in range(i, j + 1):
                        levels[k] = 0
                i = j
            i += 1
        return levels

    def _sections(self, lines: list[_Line], levels: list[int]) -> list[_Section]:
        sections: list[_Section] = []
        stack: dict[int, str] = {}
        cur = _Section(None, 0, [])
        for ln, lv in zip(lines, levels, strict=True):
            if lv == 0:
                cur.lines.append(ln)
                continue
            prev = sections[-1] if sections else None
            if (cur.lines == [] and cur.heading is not None and cur.level == lv
                    and cur.heading_lines[-1].element is ln.element):
                cur.heading += " " + ln.text      # a heading that wraps onto a second line
                cur.heading_lines.append(ln)
                cur.path[-1] = cur.heading
                continue
            if cur.heading is not None or cur.lines:
                sections.append(cur)
            stack = {k: v for k, v in stack.items() if k < lv}
            stack[lv] = ln.text
            cur = _Section(ln.text, lv, [stack[k] for k in sorted(stack)], [ln])
            del prev
        if cur.heading is not None or cur.lines:
            sections.append(cur)
        return sections

    def _paragraphs(self, sec: _Section) -> list[tuple[str, list[_Line]]]:
        paras: list[list[_Line]] = []
        for ln in sec.lines:
            new_block = not paras or paras[-1][-1].element is not ln.element
            if new_block and not (paras and LIST_ITEM.match(ln.text)):
                paras.append([ln])             # a new block starts a paragraph, unless it continues a list
            else:
                paras[-1].append(ln)
        return [(join_lines([x.text for x in p]), p) for p in paras]

    # --- records -------------------------------------------------------------------------------------------------

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        lines = self._lines()
        if not lines:
            return HandlerResult()
        body = self._body_size(lines)
        levels = self._levels(lines, body, ctx.cfg.text.heading_min_delta)
        stamp = ctx.stamp(self, prompts.PROMPT_VERSIONS)
        result = HandlerResult()
        struck_blocks: dict[str, ElementUpdate] = {}
        for sec in self._sections(lines, levels):
            paras = self._paragraphs(sec)
            heading_only_struck = bool(sec.heading_lines) and all(h.struck for h in sec.heading_lines)
            if not paras and not heading_only_struck:
                continue                           # a heading with nothing under it carries no record
            text = "\n".join(t for t, _ in paras)
            all_lines = sec.heading_lines + sec.lines
            heading_struck = bool(sec.heading_lines) and all(h.struck for h in sec.heading_lines)
            body_struck = bool(sec.lines) and all(x.struck for x in sec.lines)
            struck = heading_struck or body_struck
            stype, method, notes = self._section_type(ctx, sec, text)
            rec = Record("section", all_lines[0].element.page, stamp, {}, stype,
                         [Evidence.of(e) for e in {x.element.element_id: x.element for x in all_lines}.values()],
                         status=Status.NOT_APPLICABLE if struck else Status.VERIFIED,
                         applicability="not_applicable" if struck else "active",
                         data={"heading": sec.heading, "level": sec.level, "path": sec.path, "section_type": stype,
                               "section_type_method": method, "text": text,
                               "paragraphs": [{"text": t, "page": p[0].element.page,
                                               "element_ids": list(dict.fromkeys(x.element.element_id for x in p)),
                                               "struck": all(x.struck for x in p)} for t, p in paras],
                               "numerics": [self._numeric(n) for n in extract(text)],
                               "pages": sorted({x.element.page for x in all_lines}), "notes": notes})
            if stype and not struck:
                self._facts(ctx, rec, stype, text)
            result.records.append(rec)
            for ln in all_lines:                 # a block whose every line is struck is flagged struck
                blk_lines = ln.element.representations.get("lines", [])
                if blk_lines and all(b.get("struck") for b in blk_lines):
                    struck_blocks[ln.element.element_id] = ElementUpdate(add_flags=(Flag.STRUCK,))
        result.element_updates.update(struck_blocks)
        lg.log_event("text", "sections", sections=len(result.records), body_size=body, struck=len(struck_blocks))
        return result

    @staticmethod
    def _numeric(n: Numeric) -> dict:
        return {"kind": n.kind, "raw": n.raw, "value": n.value, "unit": n.unit, "start": n.start, "end": n.end}

    def _section_type(self, ctx: HandlerContext, sec: _Section, text: str) -> tuple[str | None, str, list[str]]:
        if sec.heading is None:
            return None, "none", []
        st = ctx.catalog.section_type(sec.heading)
        if st:
            return st, "catalog", []
        sec_catalog = "\n".join(f"{n}: {', '.join(s.heading_synonyms)}" for n, s in ctx.catalog.sections.items())
        answer = ask(ctx.model, prompts.x4_match_heading(sec_catalog, " > ".join(sec.path), text))
        if answer is None:
            return None, "unknown", ["heading not in the catalog and no model answered (X4)"]
        st, suggested, conf = check_x4(answer, ctx.catalog)
        if st is None:
            return None, "X4", [f"X4 found no catalog type (suggested: {suggested or 'none'}, confidence {conf})"]
        notes = [f"X4 confidence {conf}"]
        return st, "X4", notes

    def _facts(self, ctx: HandlerContext, rec: Record, stype: str, text: str) -> None:
        fields = ctx.catalog.section_fields(stype)
        if not fields:
            return
        numerics = extract(text)
        need_model: dict[str, FieldDef] = {}
        for fname, fd in fields.items():
            if fd.pattern_kind is None:
                need_model[fname] = fd
                continue
            hits = [n for n in numerics if n.kind == fd.pattern_kind and (fd.pattern_unit is None or n.unit == fd.pattern_unit)]
            if not hits:
                continue
            first = hits[0]
            quote = _sentence_around(text, first.start, first.end)
            fv = FieldValue(first.value, raw=first.raw, unit=fd.unit or first.unit, method="pattern", quote=quote)
            if len(hits) > 1:
                fv.status = Status.UNVERIFIED
                fv.notes.append(f"{len(hits)} matches in the section; the first is shown")
            self._judge(ctx, rec, fname, fd, fv)
        if need_model and ctx.model is not None:
            self._x5(ctx, rec, stype, need_model, text)
        elif need_model:
            lg.log_event("text", "model_skipped", prompt="X5", section_type=stype, fields=sorted(need_model))

    def _x5(self, ctx: HandlerContext, rec: Record, stype: str, fields: dict[str, FieldDef], text: str) -> None:
        listing = "\n".join(f"- {n}: {f.desc}" + (f" (unit: {f.unit})" if f.unit else "") for n, f in fields.items())
        answer = ask(ctx.model, prompts.x5_extract_facts(stype, listing, text))
        if not isinstance(answer, dict):
            return
        for fact in answer.get("facts", []):
            fname = fact.get("field") if isinstance(fact, dict) else None
            if fname not in fields or fname in rec.fields:
                continue
            fd = fields[fname]
            chk = check_fact(text, fact, fd)
            fv = FieldValue(fact.get("value"), raw=str(fact.get("quote") or ""), unit=fact.get("unit") or fd.unit,
                            method="X5", quote=str(fact.get("quote") or ""))
            if not chk.ok:
                fv.status = Status.UNVERIFIED
                fv.notes += chk.problems
            self._judge(ctx, rec, fname, fd, fv)
        others = []
        for o in answer.get("other_facts", []):
            if isinstance(o, dict) and o.get("quote"):
                others.append({"description": o.get("description"), "quote": o["quote"],
                               "quote_verbatim": check_fact(text, {"value": "", "quote": o["quote"]}, None).problems[:1] == []})
        if others:
            rec.data["other_facts"] = others

    def _judge(self, ctx: HandlerContext, rec: Record, fname: str, fd: FieldDef, fv: FieldValue) -> None:
        """X6 for critical fields that passed the code checks: only 'supported' keeps them verified."""
        if fd.critical and fv.status == Status.VERIFIED:
            answer = ask(ctx.model, prompts.x6_judge(fv.quote or "", fname, fv.value, fv.unit, fd.desc))
            if answer is None:
                fv.status = Status.UNVERIFIED
                fv.notes.append("critical field was not judged (no model)")
            else:
                ok, reason = check_x6(answer)
                if not ok:
                    fv.status = Status.UNVERIFIED
                    fv.notes.append(f"judge: {reason}")
        rec.fields[fname] = fv
