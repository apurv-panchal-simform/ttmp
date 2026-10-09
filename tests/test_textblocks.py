from tmp_pipeline.config import load_config
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.textblocks import build_text_blocks

CFG = load_config()


def blocks(pdf, tables=()):
    return build_text_blocks(read_layers(pdf)[0], list(tables), CFG)


def test_wrapped_lines_form_one_block_and_far_text_is_separate(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 400, "first line of a paragraph")
        c.drawString(72, 388, "second line of the paragraph")
        c.drawString(72, 250, "an unrelated block lower down")
    bs = blocks(make_pdf("p.pdf", [page]))
    assert [b.text for b in bs] == ["first line of a paragraph\nsecond line of the paragraph",
                                    "an unrelated block lower down"]


def test_side_by_side_boxes_are_separate_blocks_read_left_to_right(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 400, "left column text")
        c.drawString(72, 388, "left second line")
        c.drawString(420, 400, "right column text")
        c.drawString(420, 388, "right second line")
    bs = blocks(make_pdf("p.pdf", [page]))
    assert [b.text.split("\n")[0] for b in bs] == ["left column text", "right column text"]
    assert len(bs) == 2


def test_rotated_run_is_reassembled_in_reading_order(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.saveState()
        c.translate(60, 200)
        c.rotate(90)                       # reads bottom to top
        c.drawString(0, 0, "Operating hours")
        c.restoreState()
    (b,) = blocks(make_pdf("p.pdf", [page]))
    assert b.text == "Operating hours" and b.rotation == 90
    assert b.bbox[3] - b.bbox[1] > b.bbox[2] - b.bbox[0]       # taller than wide


def test_upside_down_run_reads_in_logical_order(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.saveState()
        c.translate(300, 300)
        c.rotate(180)
        c.drawString(0, 0, "FOOTPATH")
        c.restoreState()
    (b,) = blocks(make_pdf("p.pdf", [page]))
    assert b.text == "FOOTPATH" and b.rotation == 180


def test_bands_do_not_merge_with_body(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, h - 30, "TMP Reference KTS 26-1581")      # header band
        c.drawString(72, h - 100, "body text just below")
    bs = blocks(make_pdf("p.pdf", [page]))
    assert [(b.band, b.text) for b in bs] == [("header", "TMP Reference KTS 26-1581"), ("body", "body text just below")]
