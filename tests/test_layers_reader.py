import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader

from tmp_pipeline.layers.reader import read_layers


def test_chars_carry_font_size_bold_colour_and_pdf_point_position(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica-Bold", 14)
        c.setFillColorRGB(1, 0, 0)
        c.drawString(100, 300, "Hi")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(100, 200, "yo")
    layers = read_layers(make_pdf("t.pdf", [page]))
    assert len(layers) == 1 and layers[0].page == 1
    big = [ch for ch in layers[0].chars if ch.text == "H"][0]
    assert big.font.endswith("Helvetica-Bold") and big.bold is True and big.size == pytest.approx(14)
    assert tuple(round(v, 2) for v in big.colour) == (1.0, 0.0, 0.0)
    assert big.x0 == pytest.approx(100, abs=0.5)
    assert 295 <= big.y0 <= 300 and big.y1 == pytest.approx(311.1, abs=1)   # box includes descender; origin bottom-left
    small = [ch for ch in layers[0].chars if ch.text == "y"][0]
    assert small.bold is False and small.y0 < big.y0


def test_filled_rect_and_line_are_read_with_colour(make_pdf):
    def page(c, w, h):
        c.setFillColorRGB(0, 0.69, 0.31)
        c.rect(50, 50, 100, 40, stroke=0, fill=1)
        c.setStrokeColorRGB(0, 0, 0)
        c.line(10, 400, 300, 400)
    p = read_layers(make_pdf("r.pdf", [page]))[0]
    fills = [r for r in p.rects if r.fill]
    assert len(fills) == 1
    r = fills[0]
    assert (r.x0, r.y0, r.x1, r.y1) == pytest.approx((50, 50, 150, 90), abs=0.5)
    assert tuple(round(v, 2) for v in r.fill_colour) == (0.0, 0.69, 0.31)
    assert any(abs(l.y0 - 400) < 0.5 and abs(l.x1 - l.x0 - 290) < 0.5 for l in p.lines + p.rects)


def test_image_inside_form_xobject_is_placed_in_page_space(make_pdf, tmp_path):
    img = Image.new("RGB", (40, 20), (200, 10, 10))

    def page(c, w, h):
        c.beginForm("fx")
        c.drawImage(ImageReader(img), 0, 0, width=400, height=200)
        c.endForm()
        c.saveState()
        c.translate(100, 100)
        c.scale(0.5, 0.5)
        c.doForm("fx")
        c.restoreState()
    p = read_layers(make_pdf("f.pdf", [page]))[0]
    assert len(p.images) == 1
    im = p.images[0]
    assert (im.x0, im.y0, im.x1, im.y1) == pytest.approx((100, 100, 300, 200), abs=1)
    nested = [o for o in p.objects if o.kind == "image"]
    assert nested and nested[0].level == 1
    assert nested[0].bbox_pt == pytest.approx([100, 100, 300, 200], abs=1)   # raw child coords were 0..400
    assert all(0 <= v <= max(p.width, p.height) for v in nested[0].bbox_pt)


def test_acroform_checkbox_is_read(make_pdf):
    def page(c, w, h):
        c.acroForm.checkbox(name="cb1", x=100, y=100, size=20, checked=True)
        c.acroForm.checkbox(name="cb2", x=200, y=100, size=20, checked=False)
    p = read_layers(make_pdf("a.pdf", [page]))[0]
    fields = {f.name: f for f in p.fields}
    assert set(fields) == {"cb1", "cb2"}
    assert fields["cb1"].kind == "checkbox" and fields["cb1"].checked is True
    assert fields["cb2"].checked is False
    assert fields["cb1"].bbox_pt == pytest.approx([100, 100, 120, 120], abs=1)


def test_sample_pdf_pages_and_sizes(sample_pdf):
    layers = read_layers(sample_pdf)
    assert len(layers) == 25
    assert layers[0].width == pytest.approx(841.8, abs=0.1)
    assert layers[16].width < layers[16].height          # page 17 is portrait
    p20 = layers[19]
    nested = [o for o in p20.objects if o.kind == "image" and o.level == 1]
    assert nested and all(-1 <= o.bbox_pt[0] and o.bbox_pt[2] <= p20.width + 1 for o in nested)
