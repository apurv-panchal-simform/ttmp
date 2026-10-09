import pytest

from tmp_pipeline.models import (
    Element, ElementStore, bbox_pt_to_px, bbox_px_to_pt, FLAGS, TYPES, ZONES,
)

A4L = (841.8, 595.4)


def test_ids_are_deterministic_and_ordered_per_page_and_type():
    s = ElementStore("sha256:abc")
    a = s.add(4, A4L, "forms", "table_row", [0, 0, 10, 10])
    b = s.add(4, A4L, "forms", "table_row", [0, 10, 10, 20])
    c = s.add(5, A4L, "risk", "table_row", [0, 0, 10, 10])
    assert (a.element_id, b.element_id, c.element_id) == ("p4:table_row:0", "p4:table_row:1", "p5:table_row:0")
    assert a.doc_id == "sha256:abc"


def test_explicit_duplicate_id_raises():
    s = ElementStore("d")
    s.add(1, A4L, "forms", "heading", [0, 0, 1, 1], element_id="p1:heading:x")
    with pytest.raises(ValueError, match="duplicate"):
        s.add(1, A4L, "forms", "heading", [0, 0, 1, 1], element_id="p1:heading:x")


def test_unknown_type_zone_or_flag_rejected():
    s = ElementStore("d")
    with pytest.raises(ValueError, match="type"):
        s.add(1, A4L, "forms", "banana", [0, 0, 1, 1])
    with pytest.raises(ValueError, match="zone"):
        s.add(1, A4L, "nowhere", "heading", [0, 0, 1, 1])
    with pytest.raises(ValueError, match="flag"):
        s.add(1, A4L, "forms", "heading", [0, 0, 1, 1], flags=["weird"])


def test_vocabularies_match_the_plan():
    assert {"table_row", "checkbox", "diagram_sheet", "vector_symbol", "region", "text_block"} <= set(TYPES)
    assert {"struck", "boilerplate", "low_text_quality", "needs_caption", "leftover"} <= set(FLAGS)
    assert set(ZONES) == {"forms", "risk", "narrative", "diagram", "generic_standard", "blank", "unclassified"}


def test_dump_load_round_trip(tmp_path):
    s = ElementStore("d")
    s.add(2, A4L, "risk", "table", [1, 2, 3, 4], parent_id=None, links=["sheet:04"], flags=["struck"],
          representations={"text": "x"}, printed_page=2)
    s.add(2, A4L, "risk", "table_row", [1, 2, 3, 4], parent_id="p2:table:0")
    path = tmp_path / "e.json"
    s.dump(path)
    t = ElementStore.load(path)
    assert t.doc_id == "d"
    assert list(t.elements) == list(s.elements)
    assert t.elements["p2:table:0"] == s.elements["p2:table:0"]
    # counters survive so new ids continue the sequence
    assert t.add(2, A4L, "risk", "table", [0, 0, 1, 1]).element_id == "p2:table:1"


def test_point_pixel_conversion_at_200_dpi():
    h = A4L[1]
    px = bbox_pt_to_px([72, 72, 144, 144], h, 200)
    # y flips: PDF origin bottom-left, pixel origin top-left
    assert px == pytest.approx([200, (h - 144) * 200 / 72, 400, (h - 72) * 200 / 72])
    back = bbox_px_to_pt(px, h, 200)
    assert back == pytest.approx([72, 72, 144, 144])


def test_ensure_pixel_boxes_fills_only_missing_ones():
    s = ElementStore("d")
    a = s.add(1, A4L, "forms", "heading", [72, 72, 144, 144])
    b = s.add(1, A4L, "forms", "heading", [0, 0, 1, 1], bbox_px=[9, 9, 9, 9])
    s.ensure_pixel_boxes(200)
    assert a.bbox_px == pytest.approx(bbox_pt_to_px([72, 72, 144, 144], A4L[1], 200), abs=0.1)
    assert b.bbox_px == [9, 9, 9, 9]
