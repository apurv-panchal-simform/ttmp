"""PostgreSQL facts store (plan 3.4). Row building is pure code and always tested; writing needs a database and runs
only when TMP_PG_DSN is set (for example a local `docker run postgres:17`)."""
import json
import os
import re

import pytest

from test_facts import _document
from tmp_pipeline.db.writer import TYPED, build_rows, schema_sql

DSN = os.environ.get("TMP_PG_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="needs TMP_PG_DSN (a PostgreSQL database the tests may write to)")


def _columns(table):
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema_sql(), re.S)
    assert m, table
    return {re.match(r"\s*(\w+)", part).group(1) for part in m.group(1).split(",") if re.match(r"\s*\w+ ", part)}


def test_every_row_the_writer_builds_has_a_table_and_columns_in_the_schema():
    rows = build_rows(_document())
    for table, items in rows.items():
        cols = _columns(table)
        for row in items:
            assert set(row) <= cols, (table, set(row) - cols)
    for table, (_, cols) in TYPED.items():
        assert set(cols) <= _columns(table), table


def test_rows_are_typed_and_values_in_conflict_are_null_in_typed_tables():
    f = _document()
    rows = build_rows(f)
    (doc,) = rows["tmp_document"]
    assert doc["tmp_reference"] is None and doc["tmp_reference_status"] == "conflict"
    assert doc["approval_status"] == "partially_signed" and doc["order_reference"] == "DOM-1"
    (sw,) = rows["shift_window"]
    assert sw["first_sign_in"] == "09:00" and sw["day_type"] == "School days"
    (sl,) = rows["speed_limit"]
    assert (sl["speed_kmh"], sl["rp_from"], sl["date_from"]) == (30, 0.465, "2026-09-28")
    assert [(w["day_type"], w["time_from"]) for w in rows["speed_limit_window"]] == [("school_days", "09:00")]
    assert {r["conflict_id"] for r in rows["conflict"]} == {"C-001", "C-002", "C-003", "C-004"}
    assert json.loads(rows["conflict"][0]["versions"])[0]["value"] in ("KTS 26-1581", "KTS 26-1580")
    assert {r["role"] for r in rows["signoff"]} >= {"ttm_designer", "rca_other"}
    ev = [e for e in rows["field_evidence"] if e["fact_id"] == "road:0" and e["field"] == "start_rp"]
    assert ev and ev[0]["page"] == 4
    assert all(r["status"] for r in rows["fact"])


@needs_db
@pytest.mark.postgres
def test_write_is_idempotent_and_a_review_rewrite_updates_status(tmp_path):
    from tmp_pipeline.db.writer import connect, migrate, write
    from tmp_pipeline.facts.review import apply_corrections
    f = _document()
    f.doc_id = "sha256:dbtest"
    with connect(DSN) as conn:
        assert migrate(conn) == 1 and migrate(conn) == 1
        first = write(conn, f.doc_id, build_rows(f), ["designers"])
        second = write(conn, f.doc_id, build_rows(f), ["designers"])
        assert first == second
        n = conn.execute("SELECT count(*) FROM fact WHERE doc_id = %s", (f.doc_id,)).fetchone()[0]
        assert n == first["fact"]
        row = conn.execute("SELECT first_sign_in, access_groups FROM shift_window WHERE doc_id = %s",
                           (f.doc_id,)).fetchone()
        assert str(row[0]) == "09:00:00" and row[1] == ["designers"]
        assert conn.execute("SELECT approval_status FROM tmp_document WHERE doc_id = %s",
                            (f.doc_id,)).fetchone()[0] == "partially_signed"
        apply_corrections(f, {"reviewer": "R", "items": [{"ref": "C-001", "action": "correct",
                                                          "value": "KTS 26-1581"}]})
        write(conn, f.doc_id, build_rows(f), ["designers"])
        ref, status, rev = conn.execute("SELECT tmp_reference, tmp_reference_status, revision FROM tmp_document "
                                        "WHERE doc_id = %s", (f.doc_id,)).fetchone()
        assert (ref, status, rev) == ("KTS 26-1581", "reviewed", 2)
        conn.execute("DELETE FROM tmp_document WHERE doc_id = %s", (f.doc_id,))
        assert conn.execute("SELECT count(*) FROM fact WHERE doc_id = %s", (f.doc_id,)).fetchone()[0] == 0
        conn.commit()


def test_values_read_back_from_the_fact_table_keep_their_type():
    from tmp_pipeline.db.reader import _value
    row = {"value_json": None, "value_num": None, "value_text": None, "unit": None}
    assert _value({**row, "value_json": '["a", "b"]'}) == ["a", "b"]
    assert _value({**row, "value_num": 9260.0}) == 9260 and isinstance(_value({**row, "value_num": 0.0, "unit": "km"}), float)
    assert _value({**row, "value_text": "true"}) is True and _value({**row, "value_text": "Sunrise"}) == "Sunrise"


@needs_db
@pytest.mark.postgres
def test_the_postgres_store_reads_back_what_the_tools_need():
    from tmp_pipeline.config import load_config
    from tmp_pipeline.db.reader import PgFactsStore
    from tmp_pipeline.db.writer import connect, migrate, write
    from tmp_pipeline.retrieval.store import NoAccessError, User
    from tmp_pipeline.retrieval.tools import get_facts, get_status
    f = _document()
    f.doc_id = "sha256:readtest"
    with connect(DSN) as conn:
        migrate(conn)
        write(conn, f.doc_id, build_rows(f), ["designers"])
        store = PgFactsStore(conn=conn)
        assert store.docs(User()) == [] and [d.doc_id for d in store.docs(User(("designers",)))] == [f.doc_id]
        with pytest.raises(NoAccessError):
            store.facts(f.doc_id, User())
        back = store.facts(f.doc_id, User(("designers",)))
        cfg = load_config()
        (sl,) = get_facts(back, None, cfg, "speed_limit")
        assert sl.value("rp_from").value == 0.465 and sl.value("speed_kmh").value == 30
        st = get_status(back, None)
        assert st["approval"] == "partially_signed" and len(st["conflicts"]) == 4
        assert {s.tmd_id for s in back.tmd_sheets} == {s.tmd_id for s in f.tmd_sheets}
        info = store.docs(User(("designers",)))[0]
        assert info.tmp_ref == "KTS 26-1581" and "KTS 26-1580" in info.aliases
        conn.execute("DELETE FROM tmp_document WHERE doc_id = %s", (f.doc_id,))
        conn.commit()
