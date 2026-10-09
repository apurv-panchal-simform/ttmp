import json

import pytest

from tmp_pipeline.models import (
    FLAGS,
    SCHEMA_VERSION,
    TYPES,
    ZONES,
    ElementStore,
    PageInfo,
    SchemaError,
    bbox_pt_to_px,
    bbox_px_to_pt,
)
from tmp_pipeline.vocab import Zone

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
    with pytest.raises(ValueError, match="ElementType"):
        s.add(1, A4L, "forms", "banana", [0, 0, 1, 1])
    with pytest.raises(ValueError, match="Zone"):
        s.add(1, A4L, "nowhere", "heading", [0, 0, 1, 1])
    with pytest.raises(ValueError, match="Flag"):
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


def test_assignment_is_validated_not_only_add():
    s = ElementStore("d")
    e = s.add(1, A4L, "forms", "heading", [0, 0, 1, 1])
    with pytest.raises(ValueError):
        e.zone = "nowhere"
    with pytest.raises(ValueError):
        e.flags = ["weird"]
    with pytest.raises(ValueError):
        e.type = "banana"
    e.zone = "risk"
    assert e.zone == "risk" and isinstance(e.zone, Zone)


def test_retype_rekeys_and_rewrites_references_keeping_order():
    s = ElementStore("d")
    a = s.add(1, A4L, "forms", "text_block", [0, 0, 1, 1])
    b = s.add(1, A4L, "forms", "text_block", [2, 2, 3, 3], parent_id=a.element_id)
    c = s.add(1, A4L, "forms", "text_block", [4, 4, 5, 5], links=[a.element_id])
    s.retype(a, "page_furniture")
    assert a.element_id == "p1:page_furniture:0" and a.type == "page_furniture"
    assert list(s.elements) == ["p1:page_furniture:0", "p1:text_block:1", "p1:text_block:2"]
    assert b.parent_id == "p1:page_furniture:0" and c.links == ["p1:page_furniture:0"]
    assert "p1:text_block:0" not in s.elements


def test_load_after_retype_does_not_reuse_ids(tmp_path):
    s = ElementStore("d")
    only = s.add(1, A4L, "forms", "text_block", [0, 0, 1, 1])
    s.retype(only, "page_furniture")                     # no text_block element is left on the page
    s.dump(tmp_path / "e.json")
    t = ElementStore.load(tmp_path / "e.json")
    new = t.add(1, A4L, "forms", "text_block", [0, 0, 1, 1])     # used to raise or collide after a load
    assert new.element_id not in {"p1:page_furniture:0"} and len(t.elements) == 2


def test_fingerprint_ignores_sequence_number_but_not_content():
    s1, s2 = ElementStore("d"), ElementStore("d")
    x = s1.add(1, A4L, "forms", "text_block", [1, 1, 4, 4], representations={"text": "Road name"})
    s2.add(1, A4L, "forms", "text_block", [8, 8, 9, 9], representations={"text": "Extra"})
    y = s2.add(1, A4L, "forms", "text_block", [1.2, 0.8, 4.1, 4], representations={"text": "Road name"})
    assert x.element_id != y.element_id and x.fingerprint() == y.fingerprint()
    z = s2.add(1, A4L, "forms", "text_block", [1, 1, 4, 4], representations={"text": "Other"})
    assert z.fingerprint() != x.fingerprint()


def test_dump_is_versioned_utf8_and_atomic(tmp_path):
    s = ElementStore("d")
    s.pages[1] = PageInfo(width_pt=842, height_pt=595, form_name="1. TMP Planning Form")
    s.add(1, A4L, "forms", "text_block", [0, 0, 1, 1], representations={"text": "Pĝ – naïve"})
    path = tmp_path / "sub" / "e.json"
    s.dump(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION and "Pĝ – naïve" in path.read_text(encoding="utf-8")
    assert [p.name for p in path.parent.iterdir()] == ["e.json"]            # no temp file left behind
    assert ElementStore.load(path).pages[1].form_name == "1. TMP Planning Form"


def test_load_tolerates_unknown_fields_and_old_files_but_not_newer_schemas(tmp_path, caplog):
    s = ElementStore("d")
    s.pages[1] = PageInfo(width_pt=842, height_pt=595)
    s.add(1, A4L, "forms", "table", [0, 0, 1, 1])
    path = tmp_path / "e.json"
    s.dump(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["elements"][0]["future_field"] = 1
    raw["pages"]["1"]["future_page_field"] = 2
    del raw["schema_version"]                                              # a file from before versioning
    path.write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level("WARNING"):
        t = ElementStore.load(path)
    assert "p1:table:0" in t.elements and "future_field" in caplog.text and "future_page_field" in caplog.text
    raw["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SchemaError):
        ElementStore.load(path)
