import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader

from tmp_pipeline.layers.render import Renderer, perceptual_hash


def _red_box_page(c, w, h):
    c.setFillColorRGB(1, 0, 0)
    c.rect(100, 200, 80, 40, stroke=0, fill=1)


def test_master_render_size_and_webp_file(make_pdf, tmp_path):
    pdf = make_pdf("r.pdf", [_red_box_page])
    with Renderer(pdf) as r:
        path, size = r.save_master(1, tmp_path)
    assert path.suffix == ".webp" and path.exists()
    assert size == pytest.approx((841.9 * 200 / 72, 595.3 * 200 / 72), abs=2)


def test_crop_by_point_box_returns_the_right_pixels(make_pdf):
    pdf = make_pdf("r.pdf", [_red_box_page])
    with Renderer(pdf) as r:
        crop = r.crop(1, [100, 200, 180, 240], dpi=300)
        outside = r.crop(1, [300, 300, 380, 340], dpi=300)
    assert crop.size == pytest.approx((80 * 300 / 72, 40 * 300 / 72), abs=2)
    px = crop.convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert px[0] > 240 and px[1] < 15 and px[2] < 15         # all red
    assert outside.convert("RGB").resize((1, 1)).getpixel((0, 0)) == (255, 255, 255)


def test_same_image_on_two_pages_has_equal_hash_and_different_image_differs(make_pdf):
    a = Image.effect_mandelbrot((64, 64), (-2.2, -1.5, 1, 1.5), 40).convert("RGB")
    b = Image.linear_gradient("L").convert("RGB")

    def with_img(img):
        def draw(c, w, h):
            c.drawImage(ImageReader(img), 50, 50, width=200, height=200)
        return draw
    pdf = make_pdf("h.pdf", [with_img(a), with_img(a), with_img(b)])
    with Renderer(pdf) as r:
        hashes = [perceptual_hash(r.crop(i, [50, 50, 250, 250], dpi=100)) for i in (1, 2, 3)]
    assert hashes[0] == hashes[1]
    assert hashes[0] != hashes[2]
    assert len(hashes[0]) == 16
