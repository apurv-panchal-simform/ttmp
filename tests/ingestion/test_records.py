import json

import pytest

from tmp_pipeline.ingestion.models import Element, SchemaError
from tmp_pipeline.records import Evidence, FieldValue, Record, RecordStore, Stamp, Status

STAMP = Stamp("tables", "1.0.0", config_hash="abc")


def _element(eid="p4:table:0"):
    return Element(eid, "d", 4, [842, 595], "forms", "table", [0, 0, 10, 10], representations={"text": "Roads"})


def _road(**kw):
    el = _element()
    return Record("road", 4, STAMP, entity="road", evidence=[Evidence.of(el)],
                  fields={"road_name": FieldValue("Sunrise Avenue"),
                          "start_rp": FieldValue(0.41, raw="RP 0.410", unit="km")}, **kw)


def test_ids_are_per_page_and_kind_and_unique():
    s = RecordStore("d")
    a, b = s.add(_road()), s.add(_road())
    assert (a.record_id, b.record_id) == ("r4:road:0", "r4:road:1")
    with pytest.raises(ValueError, match="duplicate"):
        s.add(_road(record_id="r4:road:0"))


def test_lookup_by_element_and_fingerprint():
    s = RecordStore("d")
    rec = s.add(_road())
    assert s.records_for("p4:table:0") == [rec] and s.records_for("p9:table:0") == []
    assert s.by_fingerprint(_element("p4:table:7").fingerprint()) == [rec]       # same content, other sequence number
    assert s.of_kind("road", page=4) == [rec] and s.of_kind("road", page=5) == []


def test_status_and_flags_are_validated():
    with pytest.raises(ValueError):
        FieldValue(1, status="maybe")
    with pytest.raises(ValueError):
        Record("road", 1, STAMP, flags=["weird"])
    assert FieldValue(1, status="unverified").status == Status.UNVERIFIED


def test_content_hash_ignores_id_and_stamp_but_not_values():
    a, b = _road(), _road(record_id="r4:road:9")
    b.produced_by = Stamp("tables", "2.0.0")
    assert a.content_hash() == b.content_hash()
    b.fields["start_rp"].value = 0.5
    assert a.content_hash() != b.content_hash()


def test_round_trip_keeps_everything_and_numbering_resumes(tmp_path):
    s = RecordStore("d")
    s.add(_road(flags=["merged"], data={"cells": [[1, 2]]}))
    path = tmp_path / "records.json"
    s.dump(path)
    t = RecordStore.load(path)
    r = t.records["r4:road:0"]
    assert r.fields["start_rp"].raw == "RP 0.410" and r.flags == ["merged"] and r.produced_by == STAMP
    assert r.evidence[0].element_id == "p4:table:0" and r.data == {"cells": [[1, 2]]}
    assert t.add(_road()).record_id == "r4:road:1"
    assert t.records_for("p4:table:0")


def test_load_tolerates_unknown_fields_and_rejects_newer_schema(tmp_path):
    s = RecordStore("d")
    s.add(_road())
    path = tmp_path / "records.json"
    s.dump(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["records"][0]["future"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert len(RecordStore.load(path)) == 1
    raw["schema_version"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SchemaError):
        RecordStore.load(path)
