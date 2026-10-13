import json
import os
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader

from tmp_pipeline import logging_ as lg
from tmp_pipeline.di import run as di_run
from tmp_pipeline.di.cache import CacheMiss, DiCache, cache_key
from tmp_pipeline.di.client import DiConfig, analyze
from tmp_pipeline.di.geometry import check_page_geometry, polygon_to_bbox_pt
from tmp_pipeline.di.to_elements import di_to_elements
from tmp_pipeline.di.triggers import di_triggers
from tmp_pipeline.models import ElementStore
from tmp_pipeline.run import run as phase1

A4L = (841.8, 595.4)


def poly(x0, y0, x1, y1):
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def di_result(figure_box_in=(1.0, 1.0, 5.0, 4.0)):
    """A hand-written analyzeResult: one A4-landscape page, one figure, two words inside it, one outside."""
    return {"apiVersion": "2024-11-30", "pages": [{
        "pageNumber": 1, "angle": 0, "width": 841.8 / 72, "height": 595.4 / 72, "unit": "inch",
        "words": [
            {"content": "LG2509427B", "polygon": poly(2.0, 2.0, 3.0, 2.2), "confidence": 0.98},
            {"content": "RHB", "polygon": poly(2.0, 2.4, 2.5, 2.6), "confidence": 0.97},
            {"content": "Outside", "polygon": poly(8.0, 6.0, 9.0, 6.2), "confidence": 0.99}]}],
        "figures": [{"id": "1.1", "boundingRegions": [{"pageNumber": 1, "polygon": poly(*figure_box_in)}]}]}


def test_polygon_converts_inches_top_left_to_points_bottom_left():
    box = polygon_to_bbox_pt(poly(1.0, 1.0, 2.0, 3.0), 595.4)
    assert box == pytest.approx([72, 595.4 - 216, 144, 595.4 - 72])


def test_geometry_checks_flag_unit_size_and_angle():
    page = {"unit": "inch", "width": 841.8 / 72, "height": 595.4 / 72, "angle": 0}
    assert check_page_geometry(page, 841.8, 595.4) == []
    assert check_page_geometry({**page, "unit": "pixel"}, 841.8, 595.4)
    assert check_page_geometry(page, 595.4, 841.8)
    assert check_page_geometry({**page, "angle": 90}, 841.8, 595.4)


def test_cache_key_changes_with_every_ingredient(tmp_path):
    base = cache_key("abc", "prebuilt-layout", "2024-11-30", ["a", "b"], None)
    assert base == cache_key("abc", "prebuilt-layout", "2024-11-30", ["b", "a"], None)       # feature order is irrelevant
    for other in (cache_key("abd", "prebuilt-layout", "2024-11-30", ["a", "b"], None),
                  cache_key("abc", "prebuilt-read", "2024-11-30", ["a", "b"], None),
                  cache_key("abc", "prebuilt-layout", "2025-01-01", ["a", "b"], None),
                  cache_key("abc", "prebuilt-layout", "2024-11-30", ["a"], None),
                  cache_key("abc", "prebuilt-layout", "2024-11-30", ["a", "b"], "1-3")):
        assert other != base
    c = DiCache(tmp_path)
    with pytest.raises(CacheMiss):
        c.get(base)
    c.put(base, {"x": 1})
    assert c.get(base) == {"x": 1}


class FakeService:
    def __init__(self, result):
        self.calls, self.polls, self.result = [], 0, result

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if method == "POST":
            return 202, {"Operation-Location": "https://x.cognitiveservices.azure.com/documentintelligence/documentModels/"
                         "prebuilt-layout/analyzeResults/res-1?api-version=2024-11-30"}, b""
        self.polls += 1
        if self.polls == 1:
            return 200, {}, json.dumps({"status": "running"}).encode()
        return 200, {}, json.dumps({"status": "succeeded", "analyzeResult": self.result}).encode()


def test_analyze_calls_service_once_then_serves_from_cache_and_never_logs_the_key(tmp_path, make_pdf):
    pdf = make_pdf("d.pdf", [lambda c, w, h: c.drawString(72, 300, "hi")])
    svc = FakeService(di_result())
    cfg = DiConfig("https://x.cognitiveservices.azure.com", key="TOP-SECRET-KEY")
    log = tmp_path / "di.log"
    lg.start_run(log)
    r1 = analyze(pdf, "sha", DiCache(tmp_path / "c"), cfg, transport=svc, poll_s=0)
    r2 = analyze(pdf, "sha", DiCache(tmp_path / "c"), cfg, transport=svc, poll_s=0)
    lg.end_run()
    assert r1["pages"] == r2["pages"] and r1["_resultId"] == "res-1"
    assert [c[0] for c in svc.calls].count("POST") == 1
    post = svc.calls[0]
    assert "features=ocrHighResolution,styleFont" in post[1] and "output=figures" in post[1] and "api-version=2024-11-30" in post[1]
    assert post[2]["Ocp-Apim-Subscription-Key"] == "TOP-SECRET-KEY"       # sent to the service, never written to the log
    text = log.read_text()
    assert "TOP-SECRET-KEY" not in text
    events = [json.loads(l) for l in text.splitlines()]
    assert [e["cache_hit"] for e in events if e["event"] == "response"] == [False, True]


def test_cache_only_miss_raises_without_calling_the_service(tmp_path, make_pdf):
    pdf = make_pdf("d.pdf", [lambda c, w, h: None])
    svc = FakeService(di_result())
    with pytest.raises(CacheMiss):
        analyze(pdf, "sha", DiCache(tmp_path / "c"), DiConfig("https://x", "k"), cache_only=True, transport=svc)
    assert svc.calls == []


def core_store(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Some text with no recognisable cue at all here")
    pdf = make_pdf("c.pdf", [page])
    res = phase1(pdf, pdf.parent / "out", render=False)
    return pdf, res


def test_di_elements_keep_di_word_index_in_the_id_and_nest_words_in_figures(make_pdf):
    _, res = core_store(make_pdf)
    di = di_to_elements(di_result(), res.store)
    (fig,) = di.of_type("figure")
    assert fig.source.startswith("azure-di") and fig.bbox_pt == pytest.approx([72, 595.4 - 288, 360, 595.4 - 72], abs=0.5)
    w0 = di.elements["p1:ocr_word:0"]
    assert w0.representations["text"] == "LG2509427B" and w0.parent_id == fig.element_id
    assert di.elements["p1:ocr_word:2"].parent_id is None
    assert w0.metadata["di_index"] == 0 and w0.bbox_px


def test_region_filter_keeps_figure_words_and_words_in_regions_only(make_pdf):
    _, res = core_store(make_pdf)
    elsewhere = di_to_elements(di_result(), res.store, regions={1: [[10, 10, 50, 50]]})
    assert set(elsewhere.elements) == {"p1:figure:0", "p1:ocr_word:0", "p1:ocr_word:1"}
    # a region around the third word (8-9 in across, 6.0-6.2 in down) brings it in
    around = [8.0 * 72 - 5, 595.4 - 6.2 * 72 - 5, 9.0 * 72 + 5, 595.4 - 6.0 * 72 + 5]
    assert "p1:ocr_word:2" in di_to_elements(di_result(), res.store, regions={1: [around]}).elements
    assert "p1:ocr_word:2" in di_to_elements(di_result(), res.store).elements          # None keeps every word


def test_page_with_wrong_geometry_is_rejected_and_logged(make_pdf, tmp_path):
    _, res = core_store(make_pdf)
    bad = di_result()
    bad["pages"][0]["width"] = 5.0
    log = tmp_path / "g.log"
    lg.start_run(log)
    di = di_to_elements(bad, res.store)
    lg.end_run()
    assert not di.elements and "geometry_rejected" in log.read_text()


def test_triggers_cover_scanned_low_quality_picture_text_and_empty_table(make_pdf, tmp_path):
    logo = Image.new("RGB", (80, 60), (200, 200, 200))

    def scanned(c, w, h):
        c.rect(50, 50, 100, 100)

    def text_page(c, w, h, picture=False):
        c.setFont("Helvetica", 8)
        c.drawString(60, h - 30, "TMP Reference KTS 26-1581")
        c.drawString(60, 25, "New Zealand Guide 1. TMP Planning Form Page 1")
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "PGstrgan ManahGmGnt xqzv wvrtk lmnop")
        if picture:
            c.drawImage(ImageReader(logo), 100, 150, width=400, height=300)   # big picture, no text layer inside
        c.setLineWidth(0.5)
        for i in range(4):
            c.line(520, 100 + i * 20, 700, 100 + i * 20)
        for j in range(4):
            c.line(520 + j * 60, 100, 520 + j * 60, 160)                    # empty ruled table
    s = phase1(make_pdf("t.pdf", [scanned, lambda c, w, h: text_page(c, w, h, True), text_page, text_page]), tmp_path / "o", render=False).store
    reasons = {(t.page, t.reason) for t in di_triggers(s)}
    assert (1, "scanned_page") in reasons
    assert (2, "low_text_quality") in reasons and (2, "text_in_picture") in reasons and (2, "ambiguous_table") in reasons


def test_cli_default_run_only_evaluates_triggers_and_cache_only_builds_elements(make_pdf, tmp_path):
    pdf, res = core_store(make_pdf)
    out = di_run.run(pdf, res.out_dir)
    assert out["called"] is False and (res.out_dir / "di_triggers.json").exists()
    with pytest.raises(CacheMiss):
        di_run.run(pdf, res.out_dir, cache_only=True)
    sha = res.store.doc_id.split(":", 1)[1]
    key = cache_key(sha, "prebuilt-layout", "2024-11-30", ["ocrHighResolution", "styleFont", "figures"], None)
    DiCache(res.out_dir / "di").put(key, di_result())
    out = di_run.run(pdf, res.out_dir, cache_only=True)
    assert out["called"] is True and (res.out_dir / "di_elements.json").exists()
    assert "p1:ocr_word:0" in ElementStore.load(res.out_dir / "di_elements.json").elements


@pytest.mark.parametrize("optional", ["di", "db", "search"])
def test_core_package_does_not_import_the_optional_packages(optional):
    """Document Intelligence (di), the PostgreSQL store (db) and the live search services (search) are optional."""
    import ast
    root = Path(__file__).parent.parent / "src" / "tmp_pipeline"
    for path in root.rglob("*.py"):
        if optional in path.relative_to(root).parts[:1]:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            mods = [n.name for n in node.names] if isinstance(node, ast.Import) else \
                   [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(m == optional or m.startswith(f"{optional}.") or m.endswith(f".{optional}")
                           or f".{optional}." in m for m in mods), path


@pytest.mark.skipif(not (os.environ.get("AZURE_DI_ENDPOINT") and os.environ.get("AZURE_DI_KEY")),
                    reason="needs AZURE_DI_ENDPOINT and AZURE_DI_KEY (paid service call)")
def test_live_di_finds_the_picture_text_on_the_page_1_site_plan(sample_pdf, tmp_path):
    res = phase1(sample_pdf, tmp_path, render=False)
    di_run.run(sample_pdf, res.out_dir, live=True)
    di = ElementStore.load(res.out_dir / "di_elements.json")
    words = " ".join(e.representations["text"] for e in di.of_type("ocr_word", page=1))
    assert "LG2509427B" in words
