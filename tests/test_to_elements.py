from helpers import CFG
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.to_elements import build_elements
from tmp_pipeline.models import ElementStore


def build(pdf):
    return build_elements(read_layers(pdf), "sha256:t", CFG)


def test_text_inside_a_ruled_grid_is_a_child_of_the_table_not_a_loose_block(make_pdf):
    def page(c, w, h):
        c.setLineWidth(0.5)
        for i in range(4):
            c.line(100, 100 + i * 20, 280, 100 + i * 20)
        for j in range(4):
            c.line(100 + j * 60, 100, 100 + j * 60, 160)
        c.setFont("Helvetica", 8)
        c.drawString(105, 145, "Road name")
        c.drawString(165, 145, "Sunrise Avenue")
        c.drawString(100, 300, "Loose paragraph outside")
    s = build(make_pdf("t.pdf", [page]))
    (table,) = s.of_type("table")
    blocks = s.of_type("text_block")
    inside = [b for b in blocks if b.parent_id == table.element_id]
    loose = [b for b in blocks if b.parent_id is None]
    assert {b.representations["text"] for b in inside} == {"Road name", "Sunrise Avenue"}
    assert [b.representations["text"] for b in loose] == ["Loose paragraph outside"]


def test_every_element_has_unclassified_zone_ids_and_pixel_boxes(make_pdf):
    def page(c, w, h):
        c.drawString(72, 300, "hello there world")
        c.acroForm.checkbox(name="cb", x=100, y=100, size=15, checked=True)
    s = build(make_pdf("t.pdf", [page]))
    assert {e.zone for e in s.elements.values()} == {"unclassified"}
    assert all(e.bbox_px and e.source == "pdf-layers" for e in s.elements.values())
    (cb,) = s.of_type("checkbox")
    assert cb.metadata["state"] == "ticked" and cb.metadata["method"] == "acroform"


def test_garbled_text_is_flagged_low_quality(make_pdf):
    def page(c, w, h):
        c.drawString(72, 300, "PGstrgan ManahGmGnt xqzv wvrtk")
        c.drawString(72, 200, "Pedestrian Management plan for the site")
    s = build(make_pdf("t.pdf", [page]))
    flags = {b.representations["text"]: b.flags for b in s.of_type("text_block")}
    assert flags["PGstrgan ManahGmGnt xqzv wvrtk"] == ["low_text_quality"]
    assert flags["Pedestrian Management plan for the site"] == []


def test_store_round_trips_with_page_info(make_pdf, tmp_path):
    def page(c, w, h):
        c.drawString(72, 300, "hello there world")
    s = build(make_pdf("t.pdf", [page]))
    s.dump(tmp_path / "e.json")
    t = ElementStore.load(tmp_path / "e.json")
    assert t.pages[1].n_chars == s.pages[1].n_chars and list(t.elements) == list(s.elements)
