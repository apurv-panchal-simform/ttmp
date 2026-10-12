"""Model steps on diagram sheets and figures, driven end to end through ``run`` with fake model clients."""
import re

from reportlab.lib.utils import ImageReader

from helpers import header_footer, label, noise_image, plate, sheet_page, speed_disc
from tmp_pipeline.model.client import FakeClient
from tmp_pipeline.records import Status
from tmp_pipeline.run import run
from tmp_pipeline.vocab import ElementType, Flag


def _signs(c, w, h):
    plate(c, 200, 300, "TEMPORARY")
    speed_disc(c, 232, 318, 30)
    label(c, 190, 340, "RP 0.615", w=34)
    plate(c, 450, 300, "")                                   # an orange plate with no text: a pictogram to caption


def _ids(user: str, header: str) -> list[str]:
    block = [ln.strip() for ln in user.split(header, 1)[1].split("\n")]       # X9 puts the first word on the header line
    return [ln.split(": ", 1)[0] for ln in block if re.match(r"^p\d+:\S+: ", ln)]


def _id_of(user: str, header: str, text: str) -> list[str]:
    block = [ln.strip() for ln in user.split(header, 1)[1].split("\n")]
    return [ln.split(": ", 1)[0] for ln in block if ln.endswith(": " + text)][:1]


def _symbol_ids(user: str) -> list[str]:
    body = user.split("Extracted symbols (id, kind, text/value, RP, road, box):", 1)[1].split("Notes panel:")[0]
    return [ln.split(" | ")[0].strip() for ln in body.strip().splitlines() if " | " in ln]


def _sheet_pdf(make_pdf, **kw):
    return make_pdf("sheet.pdf", [lambda c, w, h: sheet_page(c, w, h, extra=_signs, **kw)])


def _good_x8(request):
    ids = _symbol_ids(request.user)
    return {"approaches": [{"direction": "west", "signs_in_order": ids[:2], "notes": "30 km/h at RP 0.615"}],
            "layout": [{"statement": "The work area is beside the sign.", "symbol_ids": ids[:1]}],
            "pedestrians": [], "unclear": []}


def _record(res):
    return res.records.of_kind("diagram_sheet")[0]


def test_x10_captions_pictograms_and_x8_summary_is_verified_when_grounded(make_pdf, tmp_path):
    model = FakeClient({"X10": {"caption": "worker sign", "confidence": "high"}, "X8": _good_x8})
    res = run(_sheet_pdf(make_pdf), tmp_path, model=model)
    rec = _record(res)
    assert [c["caption"] for c in rec.data["captions"].values()] == ["worker sign"]
    assert list(rec.data["captions"].values())[0]["status"] == "verified"
    assert rec.data["summary"]["verified"] is True and rec.data["summary"]["model"] == "gpt-6-sol"
    x8 = next(c for c in model.calls if c.prompt_id == "X8")
    assert len(x8.images) == 2 and "TEMPORARY" in x8.user and "green = Work Area" in x8.user
    sym = next(e for e in res.store.of_type(ElementType.VECTOR_SYMBOL) if e.metadata.get("caption"))
    assert sym.metadata["caption"]["caption"] == "worker sign"
    assert rec.produced_by.prompt_versions["X8"] == "1.0" and rec.produced_by.model == "gpt-6-sol"


def test_an_invented_symbol_or_number_in_the_summary_makes_those_claims_unverified(make_pdf, tmp_path):
    def bad(request):
        return {"approaches": [{"direction": "west", "signs_in_order": ["p1:vector_symbol:999"], "notes": "speed 45 km/h"}],
                "layout": [], "pedestrians": [], "unclear": []}
    res = run(_sheet_pdf(make_pdf), tmp_path, model=FakeClient({"X10": {"caption": "unclear", "confidence": "low"}, "X8": bad}))
    rec = _record(res)
    summary = rec.data["summary"]
    assert summary["verified"] is False and summary["approaches"]                      # kept, not edited
    problems = summary["claims"]["approaches"][0]["problems"]
    assert any("does not exist" in p for p in problems) and any("45 is not on the sheet" in p for p in problems)
    assert rec.status == Status.UNVERIFIED
    assert all(c["status"] == "unverified" for c in rec.data["captions"].values())


def test_without_a_model_the_sheet_keeps_its_vector_facts_and_is_flagged_not_guessed(make_pdf, tmp_path):
    res = run(_sheet_pdf(make_pdf), tmp_path)
    rec = _record(res)
    assert rec.data["summary"] is None and rec.data["captions"] == {} and rec.data["figures"] == []
    assert rec.fields["tmd_id"].status == Status.VERIFIED and Flag.NEEDS_CAPTION in rec.flags
    assert any(s["kind"] == "temporary_speed_limit" for s in rec.data["symbols"])


def test_the_overlay_render_and_master_are_written_for_the_sheet(make_pdf, tmp_path):
    res = run(_sheet_pdf(make_pdf), tmp_path)
    data = _record(res).data["images"]
    assert (res.out_dir / data["master"]).exists() and (res.out_dir / data["overlay"]).exists()
    assert (res.out_dir / "sheets.json").exists()


def _figure_page(seed, x=100, label_text="Main=100PE"):
    """A form page with a picture and a label drawn on it (the position differs per page: a picture in the same spot
    on most pages would be page furniture)."""
    def page(c, w, h):
        header_footer(c, w, h)
        c.drawImage(ImageReader(noise_image(seed, (300, 200))), x, 200, 300, 200)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica", 9)
        c.drawString(x + 20, 260, label_text)
    return page


def _x7(request):
    ids = _ids(request.user, "OCR words found inside this image (id: text):")
    return {"kind": "site plan", "description": "A site plan with a pipe label.",
            "labels": [{"text": "Main=100PE", "points_to": "pipe", "ocr_ids": ids[:1]}],
            "numbers": [{"value": 100, "unit": "mm", "meaning": "pipe size", "ocr_ids": ids[:1]}], "legend": []}


def test_a_figure_is_read_once_per_distinct_picture_and_grounded_in_cited_words(make_pdf, tmp_path):
    model = FakeClient({"X7": _x7})
    pdf = make_pdf("fig.pdf", [_figure_page(1, 100), _figure_page(1, 300), _figure_page(2, 450)])
    res = run(pdf, tmp_path, model=model)
    figs = res.records.of_kind("figure")
    assert len(figs) == 3 and sum(1 for c in model.calls if c.prompt_id == "X7") == 2         # the repeated picture reused
    first, twin, other = figs
    assert twin.data["reused_from"] == first.evidence[0].element_id and other.data["reused_from"] is None
    assert first.fields["description"].value.startswith("A site plan") and first.fields["description"].method == "X7"
    assert first.status == Status.VERIFIED and first.data["numbers"][0]["status"] == "verified"


def test_a_failed_figure_check_gets_a_second_read_that_must_agree_and_pass(make_pdf, tmp_path):
    def first(request):
        r = _x7(request)
        r["numbers"] = [{"value": 100, "unit": "mm", "meaning": "pipe size", "ocr_ids": []},
                        {"value": 77, "unit": "mm", "meaning": "invented", "ocr_ids": []}]
        return r

    def second(request):
        r = _x7(request)
        r["numbers"] = [{"value": 100, "unit": "mm", "meaning": "pipe size", "ocr_ids": _ids(
            request.user, "OCR words found inside this image (id: text):")[:1]}]
        return r
    calls = []

    def route(request):
        calls.append(request.model)
        return (second if request.model == "gpt-5.5" else first)(request)
    model = FakeClient({"X7": route})
    res = run(make_pdf("fig.pdf", [_figure_page(1)]), tmp_path, model=model)
    rec = res.records.of_kind("figure")[0]
    assert calls == ["gpt-6-sol", "gpt-5.5"]
    status = {n["value"]: n["status"] for n in rec.data["numbers"]}
    assert status == {100: "verified", 77: "unverified"} and rec.status == Status.UNVERIFIED
    assert rec.data["second_read"] is not None


def test_without_a_model_a_figure_is_flagged_needs_caption_and_unverified(make_pdf, tmp_path):
    res = run(make_pdf("fig.pdf", [_figure_page(1)]), tmp_path)
    rec = res.records.of_kind("figure")[0]
    assert rec.status == Status.UNVERIFIED and Flag.NEEDS_CAPTION in rec.flags and rec.fields["description"].value is None
    el = res.store.elements[rec.evidence[0].element_id]
    assert Flag.NEEDS_CAPTION in el.flags


def test_a_flattened_sheet_is_read_by_x9_and_only_agreed_grounded_values_are_verified(make_pdf, tmp_path):
    def extra(c, w, h):
        label(c, 300, 300, "30 m", w=30)

    def x9(request):
        ids = _id_of(request.user, "OCR words in this region (id: text):", "30 m")
        second = request.model == "gpt-5.5"
        d = [{"value": 30, "unit": "m", "meaning": "taper", "ocr_ids": [] if second else ids[:1]},
             {"value": 99, "unit": "m", "meaning": "invented", "ocr_ids": []}]
        return {"signs": [], "distances": d if (ids or second) else d[1:], "rps": [], "cones": [],
                "controllers": [], "pedestrians": [], "other": []}
    pdf = make_pdf("flat.pdf", [lambda c, w, h: sheet_page(c, w, h, extra=extra, filler=0, notes=False, legend=False)])
    res = run(pdf, tmp_path, model=FakeClient({"X9": x9}))
    rec = _record(res)
    assert rec.data["vector"] is False and Flag.UNVERIFIED in rec.flags
    items = [i for t in rec.data["fallback"]["tiles"] for i in t["items"]]
    assert {i["value"]: i["status"] for i in items if i["status"] == "verified"} == {30: "verified"}
    assert all(i["status"] == "unverified" for i in items if i["value"] == 99)
    assert rec.data["summary"] is None
