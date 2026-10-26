import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader

from helpers import CFG, draw_text_along, noise_image
from tmp_pipeline.ingestion.extract.diagrams.lineage import hamming, link_reuse
from tmp_pipeline.ingestion.extract.diagrams.overlay import read_overlays
from tmp_pipeline.ingestion.extract.diagrams.textlines import build_lines


def _lines(pdf, page=1):
    ov = read_overlays(pdf, [page])[page]
    return ov, build_lines(ov.objects, CFG.diagrams, CFG.quality.low_threshold, CFG.quality.min_words)


def test_overlay_reads_objects_in_drawing_order_with_colour_and_style(make_pdf):
    def page(c, w, h):
        c.setFillColorRGB(1, 1, 1)
        c.setStrokeColorRGB(0.75, 0.07, 0.12)
        c.setLineWidth(5)
        c.circle(100, 100, 10, stroke=1, fill=1)
        c.setStrokeColorRGB(0, 0, 1)
        c.setLineWidth(2)
        c.setDash(3, 2)
        c.line(10, 10, 200, 10)
    ov = read_overlays(make_pdf("o.pdf", [page]), [1])[1]
    paths = ov.of_kind("path")
    assert [p.index for p in paths] == sorted(p.index for p in paths)
    disc, dashed = paths[0], paths[-1]
    assert disc.fill == (255, 255, 255) and disc.stroke == (191, 18, 31) and disc.stroke_width == pytest.approx(5, abs=0.1)
    assert disc.bbox == pytest.approx([85, 85, 115, 115], abs=1.0)      # pdfium bounds include the stroke
    assert dashed.dashed and dashed.stroke == (0, 0, 255) and not disc.dashed


def test_image_inside_a_form_is_placed_in_page_space_and_hashed(make_pdf):
    img = noise_image(3)

    def page(c, w, h):
        c.beginForm("fx")
        c.drawImage(ImageReader(img), 0, 0, width=400, height=200)
        c.endForm()
        c.saveState()
        c.translate(100, 100)
        c.scale(0.5, 0.5)
        c.doForm("fx")
        c.restoreState()
    ov = read_overlays(make_pdf("f.pdf", [page]), [1])[1]
    im = ov.of_kind("image")[0]
    assert im.level == 1 and im.bbox == pytest.approx([100, 100, 300, 200], abs=1.5)
    assert im.px == (160, 100) and im.phash and len(im.phash) == 16


def test_rotated_glyphs_are_read_in_drawing_order_as_one_line(make_pdf):
    def page(c, w, h):
        draw_text_along(c, 200, 200, "TEMPORARY", 111)      # the sample's sign text runs at about 111 degrees
        draw_text_along(c, 300, 200, "30", 20)
    _, lines = _lines(make_pdf("r.pdf", [page]))
    assert sorted(ln.text for ln in lines) == ["30", "TEMPORARY"]
    temp = next(ln for ln in lines if ln.text == "TEMPORARY")
    assert temp.angle == pytest.approx(111, abs=8)


def test_a_label_drawn_beside_a_line_is_not_its_continuation(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 6)
        c.drawString(100, 100, "SHEET NO:")
        c.drawString(100, 60, "04")                         # below, not along the baseline
    _, lines = _lines(make_pdf("s.pdf", [page]))
    assert [ln.text for ln in lines] == ["SHEET NO:", "04"]


def test_text_with_broken_font_encoding_is_flagged_low_quality():
    from tmp_pipeline.ingestion.extract.diagrams.overlay import OverlayObject

    def glyphs(text, x, y):
        return [OverlayObject(i, "text", 0, [x + i * 5, y, x + i * 5 + 4, y + 6], text=ch, size=7.0,
                              origin=(x + i * 5, y), matrix=(7, 0, 0, 7, x + i * 5, y)) for i, ch in enumerate(text)]
    objs = glyphs("P\u011e.\u011estrgan Manah\u011em\u011ent Op\u019fon", 10, 100)
    for k, o in enumerate(objs):
        o.index = k
    objs += [OverlayObject(100 + k, o.kind, 0, o.bbox, text=o.text, size=7.0, origin=o.origin, matrix=o.matrix)
             for k, o in enumerate(glyphs("Pedestrian Management Option", 10, 60))]
    lines = build_lines(objs, CFG.diagrams, CFG.quality.low_threshold, CFG.quality.min_words)
    low = {ln.text: ln.low_quality for ln in lines}
    assert low["P\u011e.\u011estrgan Manah\u011em\u011ent Op\u019fon"] is True
    assert low["Pedestrian Management Option"] is False


def test_reused_pictures_are_linked_across_pages_by_hash(make_pdf):
    from tmp_pipeline.ingestion.extract.diagrams.models import ImageInfo

    def with_image(seed):
        def page(c, w, h):
            c.drawImage(ImageReader(noise_image(seed)), 0, 0, 200, 100)
        return page
    pdf = make_pdf("l.pdf", [with_image(1), with_image(1), with_image(2)])
    ovs = read_overlays(pdf, [1, 2, 3])
    by_page = {p: [ImageInfo(i.index, i.bbox, i.px, i.phash, "aerial") for i in ov.of_kind("image")] for p, ov in ovs.items()}
    link_reuse(by_page, CFG.diagrams.near_dup_bits)
    assert by_page[1][0].reused_on == [2] and by_page[2][0].reused_on == [1] and by_page[3][0].reused_on == []
    assert hamming("0000000000000000", "ffffffffffffffff") == 64


def test_hash_ignores_recompression_but_not_a_different_picture():
    from tmp_pipeline.ingestion.extract.layers.render import perceptual_hash
    a = noise_image(5)
    b = Image.open(__import__("io").BytesIO(_jpeg(a)))
    assert hamming(perceptual_hash(a), perceptual_hash(b)) <= CFG.diagrams.near_dup_bits
    assert hamming(perceptual_hash(a), perceptual_hash(noise_image(6))) > CFG.diagrams.near_dup_bits


def _jpeg(img):
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()
