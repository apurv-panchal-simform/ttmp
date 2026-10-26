from helpers import CFG, grid
from tmp_pipeline.ingestion.extract.layers.reader import read_layers
from tmp_pipeline.ingestion.extract.layers.to_elements import build_elements
from tmp_pipeline.ingestion.extract.zones import assign_zones


def zones_of(pdf):
    store = build_elements(read_layers(pdf), "sha256:t", CFG)
    assign_zones(store, CFG)
    return store


def header_footer(c, w, h, form="1. TMP Planning Form", page=1):
    c.setFont("Helvetica", 8)
    c.drawString(60, h - 30, "TMP Reference KTS 26-1581")
    c.drawString(60, 25, f"New Zealand Guide to Temporary Traffic Management {form} Page {page}")


def body_text(c):
    c.setFont("Helvetica", 10)
    c.drawString(72, 450, "Pedestrians will be escorted past the worksite at all times")


def rating_grid(c, x, y):
    cols = [(1, 0, 0), (0.96, 0.52, 0.17), (1, 1, 0), (0, 0.69, 0.31), (0.62, 0.98, 0.53), (1, 0, 0)]
    for i, col in enumerate(cols):
        c.setFillColorRGB(*col)
        c.rect(x + (i % 3) * 30, y + (i // 3) * 30, 30, 30, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)


def test_form_page_takes_zone_from_footer_form_name(make_pdf):
    def page(c, w, h):
        header_footer(c, w, h)
        body_text(c)
    s = zones_of(make_pdf("f.pdf", [page]))
    assert s.pages[1].zone == "forms"
    assert {e.zone for e in s.of_type("text_block")} == {"forms"}


def test_job_pack_footer_gives_narrative(make_pdf):
    def page(c, w, h):
        header_footer(c, w, h, form="2. TMP Job Pack Form")
        body_text(c)
    assert zones_of(make_pdf("n.pdf", [page])).pages[1].zone == "narrative"


def test_page_with_form_block_and_rating_grid_gets_two_zones(make_pdf):
    def page(c, w, h):
        header_footer(c, w, h)
        grid(c, 60, 300, 8, 6)                  # big ordinary table
        c.setFont("Helvetica", 8)
        c.drawString(65, 410, "Vulnerable road users")
        rating_grid(c, 100, 100)                # small rating-colour table below
        c.drawString(105, 140, "Likely")
    s = zones_of(make_pdf("two.pdf", [page]))
    assert s.pages[1].zone == "forms"
    zones = {e.representations["text"]: e.zone for e in s.of_type("text_block") if e.metadata["band"] == "body"}
    assert zones["Vulnerable road users"] == "forms" and zones["Likely"] == "risk"
    (region,) = s.of_type("region")
    assert region.zone == "risk"


def test_page_mostly_rating_table_is_risk(make_pdf):
    def page(c, w, h):
        header_footer(c, w, h)
        rating_grid(c, 100, 100)
        body_text(c)
    assert zones_of(make_pdf("r.pdf", [page])).pages[1].zone == "risk"


def test_header_and_footer_only_page_is_blank(make_pdf):
    def page(c, w, h):
        header_footer(c, w, h, form="2. TMP Job Pack Form", page=17)
    assert zones_of(make_pdf("b.pdf", [page])).pages[1].zone == "blank"


def test_no_header_band_with_template_code_is_generic_standard(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "FH.M1 where work cannot occur near a pedestrian crossing")
    assert zones_of(make_pdf("g.pdf", [page])).pages[1].zone == "generic_standard"


def test_no_signal_at_all_stays_unclassified(make_pdf):
    def page(c, w, h):
        c.setFont("Helvetica", 10)
        c.drawString(72, 450, "Some text with no recognisable cue at all here")
    s = zones_of(make_pdf("u.pdf", [page]))
    assert s.pages[1].zone == "unclassified" and s.pages[1].zone_reason


def test_big_image_with_overlay_paths_and_no_header_is_diagram(make_pdf):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    img = Image.new("RGB", (50, 50), (120, 160, 120))

    def page(c, w, h):
        c.drawImage(ImageReader(img), 20, 20, width=w - 40, height=h - 40)
        c.setStrokeColorRGB(1, 0, 0)
        for i in range(20):
            c.line(50 + i * 5, 100, 60 + i * 5, 200)
        c.setFont("Helvetica", 8)
        c.drawString(60, 30, "SHEET NO: 01")
    s = zones_of(make_pdf("d.pdf", [page]))
    assert s.pages[1].zone == "diagram"
    (sheet,) = s.of_type("diagram_sheet")
    assert sheet.zone == "diagram" and {e.zone for e in s.elements.values()} == {"diagram"}


def test_a_flattened_sheet_with_a_sheet_no_title_block_is_still_a_diagram(make_pdf):
    from helpers import sheet_page
    pdf = make_pdf("flat.pdf", [lambda c, w, h: sheet_page(c, w, h, filler=0, notes=False, legend=False)])
    store = zones_of(pdf)
    info = store.pages[1]
    assert info.zone == "diagram" and "flattened" in info.zone_reason and info.n_paths < CFG.zones.diagram_min_paths


def test_a_big_image_with_few_paths_and_no_sheet_no_is_not_a_diagram(make_pdf):
    from reportlab.lib.utils import ImageReader

    from helpers import noise_image

    def page(c, w, h):
        c.drawImage(ImageReader(noise_image(1)), 14, 96, w - 28, h - 110)
    store = zones_of(make_pdf("photo.pdf", [page]))
    assert store.pages[1].zone != "diagram"
