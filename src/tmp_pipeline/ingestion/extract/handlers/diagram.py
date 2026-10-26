"""Diagram handler (plan section 2.6): one ``diagram_sheet`` record per sheet, built from what the diagram stage read.

The pre-router stage (``diagrams.stage``) did the reading and added the symbol, legend, notes and region elements;
this handler turns each sheet model into a record in the plan's shape (tmd id, title, rev, roads, legend, symbols,
notes, images, aliases). Everything the vector overlay gave is stored ``verified`` with method ``vector``; a value
read from text with broken font encoding is ``unverified`` with a note.

With a model it adds, per sheet and after the checks in ``model.validators``: X10 captions for pictograms, the X8
spatial summary, X7 for the pictures beside the plan, and X9 on tiles when the sheet has no vector overlay. Without a
model those stay empty and the sheet is flagged, never guessed.
"""
from __future__ import annotations

from dataclasses import asdict

from ....llm import map_parallel
from ....records import Evidence, FieldValue, Record, Status
from ... import prompts
from ...contracts import BaseHandler, ElementUpdate, HandlerContext, HandlerResult
from ...models import Element
from ...numerics import extract
from ...vocab import ElementType, Flag, Route
from ..diagrams.describe import describe
from ..diagrams.models import SheetModel
from . import diagram_model as dm
from .image_full import read_sheet


def tmd_id(sheet_no: str | None) -> str | None:
    if sheet_no and sheet_no.strip().isdigit():
        return f"TMD-{int(sheet_no):02d}"
    return None


def aliases(sheet_no: str | None, title: str | None) -> list[str]:
    """The ways a sheet is named in the document ("TMD 04: STOP GO", "TMD Sheet 04 Stop Go", "TMD4")."""
    if not (sheet_no and sheet_no.strip().isdigit()):
        return []
    n = int(sheet_no)
    out = [f"TMD {n:02d}", f"TMD{n}", f"TMD-{n:02d}", f"TMD Sheet {n:02d}"]
    if title:
        out = [f"TMD {n:02d}: {title}", f"TMD Sheet {n:02d} {title.title()}", *out]
    return out


class DiagramHandler(BaseHandler):
    route = Route.DIAGRAM
    version = "1.1.0"

    def __init__(self) -> None:
        self._sheets: list[Element] = []

    def begin(self, ctx: HandlerContext) -> None:
        self._sheets = []

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        if element.type == ElementType.DIAGRAM_SHEET:
            self._sheets.append(element)          # model steps need every sheet's render: built in finish()
        return HandlerResult()

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        res = HandlerResult()
        todo = []
        for element in self._sheets:
            sheet = ctx.sheets.get(element.page) if ctx.sheets is not None else None
            if sheet is None:
                res.note = "no sheet model (diagram stage off)"
                continue
            todo.append((element, sheet))
        # The sheets are built side by side (each only changes its own sheet model); records are added in sheet order.
        for rec in map_parallel(lambda t: self._record(ctx, *t), todo):
            res.records.append(rec)
            for sid, cap in rec.data["captions"].items():
                res.element_updates[sid] = ElementUpdate(metadata={"caption": cap})
        return res

    def _record(self, ctx: HandlerContext, element: Element, sheet: SheetModel) -> Record:
        used = {k: prompts.PROMPT_VERSIONS[k] for k in ("X7", "X8", "X9", "X10", "X13")} if ctx.model is not None else {}
        stamp = ctx.stamp(self, used, prompts.MODELS["X8"] if ctx.model is not None else "none")
        by_role = {r.kind: r for r in sheet.regions}
        evidence = [Evidence.of(element)]
        for kind in ("title_block", "legend", "notes"):
            r = by_role.get(kind)
            if r is not None and r.element_id in ctx.store.elements:
                evidence.append(Evidence.of(ctx.store.elements[r.element_id]))
        fields: dict[str, FieldValue] = {}
        problems: list[str] = []

        def put(name: str, value, raw: str | None = None, unit: str | None = None, ok: bool = True, note: str = "") -> None:
            fields[name] = FieldValue(value, raw, unit, Status.VERIFIED if ok and value not in (None, "") else Status.UNVERIFIED,
                                      "vector", None, [note] if note else [])
            if fields[name].status == Status.UNVERIFIED:
                problems.append(name)

        tid = tmd_id(sheet.sheet)
        put("tmd_id", tid, sheet.sheet, None, tid is not None, "no numeric SHEET NO in the title block" if tid is None else "")
        put("sheet", sheet.sheet, sheet.sheet)
        put("title", sheet.title, sheet.title)
        put("rev", sheet.rev, sheet.rev)
        tb = sheet.title_block
        if "date" in tb:
            nums = extract(tb["date"], ("date",))
            put("date", nums[0].value if nums else tb["date"], tb["date"], None, bool(nums))
        for key in ("tmp_number", "drawn_by", "scale"):
            if key in tb:
                put(key, tb[key], tb[key])
        flags = []
        if sheet.low_quality_lines:
            flags.append(Flag.LOW_TEXT_QUALITY)
        if any(s.needs_caption for s in sheet.symbols):
            flags.append(Flag.NEEDS_CAPTION)
        captions: dict[str, dict] = {}
        summary = None
        figures: list[dict] = []
        fallback = None
        full = None
        if ctx.model is not None and sheet.vector:
            captions = dm.caption_symbols(ctx, element, sheet)          # X8 and X13 need the captions
        for s in sheet.symbols:
            if s.id in captions:
                s.extra["caption"] = captions[s.id]
        facts = describe(sheet, (element.page_size_pt[0], element.page_size_pt[1]), ctx.cfg.diagrams.describe)
        if ctx.model is not None:
            # The three model steps do not depend on each other: run them side by side.
            steps = [("summary", lambda: dm.summarise(ctx, element, sheet, captions)) if sheet.vector
                     else ("fallback", lambda: dm.read_flattened(ctx, element, sheet)),
                     ("figures", lambda: dm.read_figures(ctx, element, sheet)),
                     ("full", lambda: read_sheet(ctx, element, sheet, facts, dm.legend_text(sheet)))]
            done = dict(map_parallel(lambda step: (step[0], step[1]()), steps))
            summary, fallback, figures, full = done.get("summary"), done.get("fallback"), done["figures"], done["full"]
        if not sheet.vector:
            flags.append(Flag.UNVERIFIED)
            problems.append("no vector overlay")
        if summary is not None and not summary["verified"]:
            problems.append("summary has unverified claims")
        data = {
            "tmd_id": tid, "sheet": sheet.sheet, "title": sheet.title, "rev": sheet.rev, "page": sheet.page,
            "aliases": aliases(sheet.sheet, sheet.title), "roads": sheet.roads, "vector": sheet.vector,
            "legend": [{"swatch": e.swatch, "meaning": e.meaning} for e in sheet.legend],
            "symbols": [asdict(s) for s in sheet.symbols], "notes": sheet.notes, "callouts": sheet.callouts,
            "regions": [asdict(r) for r in sheet.regions], "title_block": sheet.title_block,
            "low_quality_lines": sheet.low_quality_lines, "captions": captions, "summary": summary,
            "figures": figures, "fallback": fallback, "describe": {"facts": facts, "read": full},
            "images": {"master": sheet.master, "overlay": sheet.overlay, "placed": [asdict(i) for i in sheet.images]},
        }
        return Record("diagram_sheet", sheet.page, stamp, fields, "tmd_sheet", evidence, [], flags,
                      Status.UNVERIFIED if problems else Status.VERIFIED, "active", data)
