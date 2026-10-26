"""Reading one picture with X7 and deciding what may be trusted (plan section 2.7).

First read (gpt-6-sol), then the code checks: every number and label must cite words that exist, hold it and sit
together. A value that fails the checks, and every value of a critical picture, gets a second read by a different
model generation; a failed value is verified only when the second read reports the same value and passes the checks
itself. Otherwise both stay ``unverified``. Nothing the model says is ever corrected by code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ....llm import ask
from ... import prompts
from ...contracts import HandlerContext
from ...models import Element
from ...validators import ImageCheck, check_x7
from .imageio import crop_master, master_path, nearby_text, open_image, png_bytes, words_in


@dataclass
class ImageReading:
    status: str                                    # read | skipped
    result: dict | None = None
    second: dict | None = None
    check: ImageCheck | None = None
    items: list[dict] = field(default_factory=list)  # every label and number with its status
    notes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        return self.status == "read" and all(i["status"] == "verified" for i in self.items)


def _items(first: dict, check: ImageCheck, second_ok: dict[tuple[str, str], bool]) -> list[dict]:
    out: list[dict] = []
    labels = [x for x in first.get("labels") or [] if isinstance(x, dict)]
    numbers = [x for x in first.get("numbers") or [] if isinstance(x, dict)]
    srcs = [("label", x) for x in labels] + [("number", x) for x in numbers]
    for (what, src), chk in zip(srcs, check.items, strict=True):
        value = src.get("text") if what == "label" else src.get("value")
        ok = chk.ok or second_ok.get((what, str(value)), False)
        notes = [] if chk.ok else (["confirmed by the second read"] if ok else chk.problems)
        out.append({"what": what, "value": value, "unit": src.get("unit"), "meaning": src.get("meaning") or src.get("points_to"),
                    "ocr_ids": src.get("ocr_ids", []), "status": "verified" if ok else "unverified", "notes": notes})
    return out


def read_image(ctx: HandlerContext, element: Element, bbox_pt, *, heading: str = "", title: str = "",
               critical: bool = False) -> ImageReading:
    """X7 on the crop of ``bbox_pt`` from the page master. Returns ``skipped`` when no render or no model can answer."""
    master = open_image(master_path(ctx, element.page))
    if master is None:
        return ImageReading("skipped", notes=["no master render to crop from"])
    crop = crop_master(master, bbox_pt, element.page_size_pt[1])
    png = png_bytes(crop)
    words = words_in(ctx, element.page, bbox_pt)
    wd = {w.id: w for w in words}
    pdf_text = " ".join(w.text for w in words)
    ocr = [(w.id, w.text) for w in words]
    first = ask(ctx.model, prompts.x7_read_image(png, heading, nearby_text(ctx, element.page, bbox_pt), title, ocr))
    if first is None:
        return ImageReading("skipped", notes=["no model answer"])
    check = check_x7(first, wd, pdf_text)
    reading = ImageReading("read", first, None, check, models=[prompts.MODELS["X7"]])
    second_ok: dict[tuple[str, str], bool] = {}
    if not check.ok or critical:
        second = ask(ctx.model, prompts.x7_read_image(png, heading, nearby_text(ctx, element.page, bbox_pt), title, ocr,
                                                      second_read=True))
        if second is not None:
            reading.second = second
            reading.models.append(prompts.READ2_MODEL)
            c2 = check_x7(second, wd, pdf_text)
            seconds = [("label", str(x.get("text"))) for x in second.get("labels") or [] if isinstance(x, dict)] + \
                      [("number", str(x.get("value"))) for x in second.get("numbers") or [] if isinstance(x, dict)]
            for key, chk in zip(seconds, c2.items, strict=True):
                second_ok[key] = chk.ok
    reading.items = _items(first, check, second_ok)
    if check.problems:
        reading.notes += check.problems
    return reading
