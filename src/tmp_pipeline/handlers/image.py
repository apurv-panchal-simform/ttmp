"""Figure handler (plan section 2.7): X7 reads every informative figure or photo that is not part of a diagram sheet.

One ``figure`` record per picture. A caption is generated once per distinct picture (perceptual hash) and reused where
the same picture appears again. Without a model, or without a master render to crop from, the record is ``unverified``
and the element is flagged ``needs_caption``: nothing is invented.
"""
from __future__ import annotations

from ..contracts import BaseHandler, ElementUpdate, HandlerContext, HandlerResult
from ..diagrams.lineage import hamming
from ..model import prompts
from ..models import Element
from ..records import Evidence, FieldValue, Record, Status
from ..vocab import Flag, Route
from .image_full import read_picture
from .imageread import ImageReading, read_image


def figure_record(ctx: HandlerContext, stamp, element: Element, reading: ImageReading, *, phash: str | None,
                  reused_from: str | None = None, kind: str = "figure", full: dict | None = None) -> Record:
    """A ``figure`` record from one reading (shared by plain figures and the figures drawn on diagram sheets)."""
    fields: dict[str, FieldValue] = {}
    flags = []
    data: dict = {"phash": phash, "reused_from": reused_from, "notes": reading.notes, "models": reading.models,
                  "full": full}
    if reading.status == "read" and reading.result is not None:
        r = reading.result
        ok = reading.all_verified and bool(reading.check and reading.check.ok)
        status = Status.VERIFIED if ok else Status.UNVERIFIED
        notes = list(reading.notes)
        fields["image_kind"] = FieldValue(r.get("kind"), None, None, status, "X7", None, notes)
        fields["description"] = FieldValue(r.get("description"), None, None, status, "X7", None, notes)
        data.update(labels=[i for i in reading.items if i["what"] == "label"],
                    numbers=[i for i in reading.items if i["what"] == "number"], legend=r.get("legend") or [],
                    second_read=reading.second)
        rec_status = status
    else:
        flags.append(Flag.NEEDS_CAPTION)
        rec_status = Status.UNVERIFIED
        fields["description"] = FieldValue(None, None, None, Status.UNVERIFIED, "X7", None, reading.notes)
    return Record(kind, element.page, stamp, fields, None, [Evidence.of(element)], [], flags, rec_status, "active", data)


class FigureHandler(BaseHandler):
    route = Route.FIGURE
    version = "1.0.0"

    def __init__(self) -> None:
        self._figures: list[Element] = []

    def begin(self, ctx: HandlerContext) -> None:
        self._figures = []

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        self._figures.append(element)
        return HandlerResult()

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        stamp = ctx.stamp(self, {k: prompts.PROMPT_VERSIONS[k] for k in ("X7", "X13")}, prompts.MODELS["X7"])
        seen: list[tuple[str, ImageReading, str, dict | None]] = []      # (phash, reading, first element id, full read)
        res = HandlerResult()
        for el in self._figures:
            phash = el.metadata.get("phash")                 # of the picture's own pixels, set by the diagram stage
            twin = next(((r, eid, f) for h, r, eid, f in seen if phash and hamming(h, phash) <= ctx.cfg.diagrams.near_dup_bits),
                        None)
            if twin is not None:
                reading, reused, full = twin
            else:
                reading, reused = read_image(ctx, el, el.bbox_pt), None
                full = read_picture(ctx, el, el.bbox_pt, scope="a photo or figure in a form of the TMP")
                if phash:
                    seen.append((phash, reading, el.element_id, full))
            rec = figure_record(ctx, stamp, el, reading, phash=phash, reused_from=reused, full=full)
            res.records.append(rec)
            if rec.flags:
                res.element_updates[el.element_id] = ElementUpdate(tuple(rec.flags))
        return res
