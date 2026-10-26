"""Checkbox handler (plan section 2.4): pair every box with its label and say what the form answered.

Detection (form fields, tick glyphs, vector squares) already happened in Phase 1; here each box is paired
with text, in this order of layouts:

  glyph line     the box is a glyph inside a line of text, so the text splits at the glyphs:
                 "Controlled parking ☐ Traffic signals ☒"  -> each box takes the text before it (row of pairs);
                 "Yes ☐ No ☒"                              -> a Yes/No pair (below);
                 "☐ Not present onsite"                    -> label from the left, trailing text is a note
  label-then-box nearest text line to the left on the same row ("Pedestrians: ☒")
  other / note   text to the right that is not itself a label (does not end with ":") is the box's note
  lone box       no label beside it: the text on the row is the label ("Tick this box if ...")

A Yes/No pair is one answer: the ticked box. An unticked checklist box is stored as ``ticked: false`` and
answered "not ticked on the form", because that can mean no, not applicable or not filled in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ....records import Evidence, FieldValue, Record, Status
from ...catalog import normalise
from ...contracts import BaseHandler, HandlerContext, HandlerResult
from ...geometry import centre, contains
from ...models import Element
from ...vocab import ElementType, Route

NOT_TICKED = "not ticked on the form"
_CHECKLIST_ENTITIES = ("vulnerable_user", "road_aspect")


@dataclass
class _Line:
    element: Element
    text: str
    bbox: list[float]


@dataclass
class _Box:
    cb: Element
    label: str
    layout: str
    note: str | None = None
    sources: list[Element] = field(default_factory=list)      # elements whose text gave the label and note
    problem: str | None = None

    @property
    def ticked(self) -> bool:
        return self.cb.metadata.get("state") == "ticked"


def snake(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:60].strip("_") or "unlabelled"


def _same_row(a: list[float], b: list[float], slack: float) -> bool:
    return a[1] - slack <= b[3] and a[3] + slack >= b[1]


class CheckboxHandler(BaseHandler):
    route = Route.CHECKBOX
    version = "1.0.0"

    def __init__(self) -> None:
        self._pending: list[_Box] = []
        self._lines: dict[int, list[_Line]] = {}

    def begin(self, ctx: HandlerContext) -> None:
        self._pending, self._lines = [], {}

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        self._pending.append(self._pair(ctx, element))
        return HandlerResult()                  # records come from finish(): a Yes/No pair needs both boxes

    # --- pairing -------------------------------------------------------------------------------------------------

    def _page_lines(self, ctx: HandlerContext, page: int) -> list[_Line]:
        if page not in self._lines:
            out = []
            for e in ctx.store.of_type(ElementType.TEXT_BLOCK, page=page):
                if e.metadata.get("rotation", 0) not in (0, 180):
                    continue
                for ln in e.representations.get("lines", []):
                    out.append(_Line(e, ln["text"], ln["bbox"]))
            self._lines[page] = out
        return self._lines[page]

    def _glyphs(self, ctx: HandlerContext) -> str:
        return "".join(ctx.cfg.ticks.ticked + ctx.cfg.ticks.unticked)

    def _pair(self, ctx: HandlerContext, cb: Element) -> _Box:
        ck, glyphs = ctx.cfg.checkboxes, self._glyphs(ctx)
        lines = self._page_lines(ctx, cb.page)
        gl_rx = re.compile(f"[{re.escape(glyphs)}]")
        box = cb.bbox_pt
        glyph_line = next((ln for ln in lines if gl_rx.search(ln.text) and contains(ln.bbox, box, pad=2)), None)
        if glyph_line is not None:
            siblings = sorted((c for c in ctx.store.of_type(ElementType.CHECKBOX, page=cb.page)
                               if contains(glyph_line.bbox, c.bbox_pt, pad=2)), key=lambda c: c.bbox_pt[0])
            parts = [p.strip() for p in gl_rx.split(glyph_line.text)]
            if len(siblings) == len(parts) - 1:
                k = [c.element_id for c in siblings].index(cb.element_id)
                before, after = parts[k], parts[k + 1]
                if before:
                    layout = "row_of_pairs" if len(siblings) > 1 else "label_then_box"
                    return _Box(cb, before, layout, None, [glyph_line.element])
                left = self._left_label(box, lines, ck.label_gap_pt, ck.row_tol_pt, exclude=glyph_line)
                if left is not None:
                    note = None if after else self._right_note(box, lines, ck.note_gap_pt, ck.row_tol_pt, glyphs)
                    if note is not None:
                        layout = "other_box" if normalise(left.text).startswith("other") else "box_with_note"
                        return _Box(cb, left.text, layout, note.text, [left.element, glyph_line.element, note.element])
                    return _Box(cb, left.text, "box_with_note" if after else "label_then_box", after or None,
                                [left.element, glyph_line.element])
                return self._lone(box, cb, lines, ck.row_tol_pt, glyphs, extra=glyph_line)
        left = self._left_label(box, lines, ck.label_gap_pt, ck.row_tol_pt)
        if left is None:
            return self._lone(box, cb, lines, ck.row_tol_pt, glyphs)
        note = self._right_note(box, lines, ck.note_gap_pt, ck.row_tol_pt, glyphs)
        layout = "label_then_box"
        if note is not None:
            layout = "other_box" if normalise(left.text).startswith("other") else "box_with_note"
        return _Box(cb, left.text, layout, note.text if note else None,
                    [left.element] + ([note.element] if note else []))

    def _left_label(self, box, lines: list[_Line], max_gap: float, slack: float, exclude: _Line | None = None):
        cands = [ln for ln in lines if ln is not exclude and _same_row(ln.bbox, box, slack)
                 and ln.bbox[2] <= box[0] + 2 and box[0] - ln.bbox[2] <= max_gap and ln.text.strip()]
        return max(cands, key=lambda ln: ln.bbox[2], default=None)

    def _right_note(self, box, lines: list[_Line], max_gap: float, slack: float, glyphs: str):
        # a note is a short one-line block; a multi-line paragraph beside the box is the next cell's text
        cands = [ln for ln in lines if _same_row(ln.bbox, box, slack) and box[2] - 2 <= ln.bbox[0] <= box[2] + max_gap
                 and not ln.text.rstrip().endswith(":") and not any(g in ln.text for g in glyphs)
                 and len(ln.element.representations.get("lines", [])) == 1]
        return min(cands, key=lambda ln: ln.bbox[0], default=None)

    def _lone(self, box, cb: Element, lines: list[_Line], slack: float, glyphs: str, extra: _Line | None = None) -> _Box:
        row = sorted((ln for ln in lines if ln is not extra and _same_row(ln.bbox, box, slack)
                      and not any(g in ln.text for g in glyphs)), key=lambda ln: ln.bbox[0])
        if not row:
            return _Box(cb, "", "unlabelled", None, [], "no text on the box's row")
        return _Box(cb, " ".join(ln.text for ln in row), "lone_box", None, [ln.element for ln in row])

    # --- records -------------------------------------------------------------------------------------------------

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        yes, no = set(ctx.cfg.checkboxes.yes_labels), set(ctx.cfg.checkboxes.no_labels)
        boxes = sorted(self._pending, key=lambda b: (b.cb.page, -centre(b.cb.bbox_pt)[1], b.cb.bbox_pt[0]))
        used: set[str] = set()
        records: list[Record] = []
        stamp = ctx.stamp(self)
        for i, b in enumerate(boxes):
            if b.cb.element_id in used or normalise(b.label) not in yes:
                continue
            partner = next((o for o in boxes[i + 1:] if o.cb.page == b.cb.page and normalise(o.label) in no
                            and _same_row(o.cb.bbox_pt, b.cb.bbox_pt, ctx.cfg.checkboxes.row_tol_pt)), None)
            if partner is None:
                continue
            used |= {b.cb.element_id, partner.cb.element_id}
            records.append(self._yes_no(ctx, b, partner, stamp))
        for b in boxes:
            if b.cb.element_id not in used:
                records.append(self._single(ctx, b, stamp))
        return HandlerResult(records=records)

    def _evidence(self, b: _Box) -> list[Evidence]:
        ev = [Evidence.of(b.cb, b.label or None)]
        ev += [Evidence.of(e) for e in b.sources if e.element_id != b.cb.element_id]
        return ev

    def _single(self, ctx: HandlerContext, b: _Box, stamp) -> Record:
        cat = ctx.catalog.match_entity([b.label], _CHECKLIST_ENTITIES) if b.label else None
        entity, fname = (cat.entity, cat.mapped[b.label]) if cat else (None, snake(b.label))
        status = Status.UNVERIFIED if b.problem else Status.VERIFIED
        notes = [b.problem] if b.problem else []
        fields = {fname: FieldValue(b.ticked, raw=b.label or None, status=status, method="code", notes=notes)}
        if b.note:
            fields[f"{fname}_note"] = FieldValue(b.note, raw=b.note, method="code")
        return Record("checkbox", b.cb.page, stamp, fields, entity, self._evidence(b), status=status,
                      data={"label": b.label, "layout": b.layout, "ticked": b.ticked, "note": b.note,
                            "answer": "ticked" if b.ticked else NOT_TICKED, "detected_by": b.cb.metadata.get("method")})

    def _yes_no(self, ctx: HandlerContext, yes: _Box, no: _Box, stamp) -> Record:
        lines = self._page_lines(ctx, yes.cb.page)
        left_edge = min(yes.cb.bbox_pt[0], no.cb.bbox_pt[0])
        glyph_lines = {id(s) for s in yes.sources + no.sources}
        cands = [ln for ln in lines if _same_row(ln.bbox, yes.cb.bbox_pt, ctx.cfg.checkboxes.row_tol_pt)
                 and ln.bbox[2] <= left_edge + 2 and id(ln.element) not in glyph_lines and ln.text.strip()]
        question = max(cands, key=lambda ln: ln.bbox[2], default=None)
        q_text = question.text if question else ""
        problem = None
        if yes.ticked == no.ticked:
            problem = "both boxes are ticked" if yes.ticked else "neither box is ticked"
        answer = None if problem else ("yes" if yes.ticked else "no")
        status = Status.UNVERIFIED if problem or not q_text else Status.VERIFIED
        notes = [n for n in (problem, None if q_text else "no question text found on the row") if n]
        ev = self._evidence(yes) + [e for e in self._evidence(no) if e.element_id != yes.cb.element_id]
        if question is not None:
            ev.append(Evidence.of(question.element, q_text))
        return Record("yes_no", yes.cb.page, stamp,
                      {snake(q_text) if q_text else "unlabelled_question": FieldValue(
                          answer, raw=q_text or None, status=status, method="code", notes=notes)},
                      None, ev, status=status,
                      data={"question": q_text, "answer": answer or "not answered on the form", "layout": "yes_no",
                            "yes_ticked": yes.ticked, "no_ticked": no.ticked})
