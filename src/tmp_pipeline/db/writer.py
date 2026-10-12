"""Write a run's facts to PostgreSQL (plan section 3.4).

``build_rows`` turns ``facts.json`` (and the diagram symbols in ``records.json``) into rows per table; it is pure
code. ``write`` replaces the document's rows in one transaction: the ``tmp_document`` row is deleted (every other
table cascades) and everything is inserted again, so writing twice gives the same rows. psycopg is imported only
when a connection is opened, so the core never needs it; the DSN comes from ``TMP_PG_DSN`` and is never logged.
"""
from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

from ..errors import PipelineError
from ..facts.model import Fact, Facts
from ..records import RecordStore, Status

DSN_ENV = "TMP_PG_DSN"

# typed tables: table -> (entity, {column: sql type}); a column takes the fact field of the same name
TYPED: dict[str, tuple[str, dict[str, str]]] = {
    "road": ("road", {"road_name": "text", "suburb": "text", "start_rp": "float", "end_rp": "float",
                      "onrc_class": "text", "posted_speed_kmh": "int", "aadt": "int", "hmv_pct": "float",
                      "peak_periods": "text"}),
    "shift_window": ("shift_window", {"day_type": "text", "first_sign_in": "time", "tc_in": "time",
                                      "pick_up": "time", "site_cleared": "time"}),
    "work_stage": ("stage", {"tmd_id": "text", "title": "text", "hours": "text"}),
    "speed_limit": ("speed_limit", {"road_name": "text", "speed_kmh": "int", "length_m": "float",
                                    "rp_from": "float", "rp_to": "float", "date_from": "date", "date_to": "date",
                                    "tmd_ids": "text[]"}),
    "authorisation": ("authorisation", {"items": "text[]"}),
    "contact": ("contact", {"name": "text", "roles": "text[]", "organisation": "text", "phone": "text",
                            "email": "text"}),
    "risk_matrix_cell": ("risk_matrix_cell", {"likelihood": "text", "impact": "text", "score": "int",
                                              "rating": "text"}),
    "risk": ("risk", {c: "text" for c in ("category", "hazard", "groups_at_risk", "potential_event",
                                          "initial_likelihood", "initial_consequence", "initial_risk", "controls",
                                          "residual_likelihood", "residual_consequence", "residual_risk")}),
    "section": ("section", {"heading": "text", "section_type": "text"}),
    "figure": ("figure", {"image_kind": "text", "description": "text"}),
}
CAST = {"text": "text", "int": "integer", "float": "double precision", "time": "time", "date": "date",
        "text[]": "text[]", "int[]": "integer[]", "jsonb": "jsonb", "bool": "boolean"}


class DbError(PipelineError):
    """The database is not configured or psycopg is not installed."""


def schema_sql() -> str:
    return resources.files("tmp_pipeline.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _coerce(value: Any, sql: str) -> Any:
    if value is None:
        return None
    if sql == "int":
        return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
    if sql == "float":
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
    if sql == "time":
        return value if isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value) else None
    if sql == "date":
        return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    if sql == "text[]":
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _typed(f: Fact, cols: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"fact_id": f.fact_id}
    for col, sql in cols.items():
        fv = f.fields.get(col)
        out[col] = None if fv is None or fv.status == Status.CONFLICT else _coerce(fv.value, sql)
    return out


def build_rows(facts: Facts, records: RecordStore | None = None) -> dict[str, list[dict[str, Any]]]:
    """Rows per table (without doc_id and access_groups, which ``write`` adds to every row)."""
    rows: dict[str, list[dict[str, Any]]] = {t: [] for t in (
        "tmp_document", "fact", "field_evidence", *TYPED, "speed_limit_window", "tmd_sheet", "tmd_mention",
        "tmd_symbol", "risk_tmd", "checkbox", "signoff", "conflict", "check_result", "review_item", "coverage")}
    doc = next(iter(facts.of("tmp_document")), None)
    ref = doc.fields.get("tmp_reference") if doc else None
    order = doc.fields.get("order_reference") if doc else None
    rows["tmp_document"].append({
        "tmp_reference": ref.value if ref else None, "tmp_reference_status": str(ref.status) if ref else None,
        "order_reference": order.value if order else None,
        "approval_status": facts.approval.status if facts.approval else None, "revision": facts.revision,
        "config_hash": facts.config_hash, "catalog_version": facts.catalog_version,
        "versions": json.dumps(facts.versions)})
    for f in facts.facts:
        for fname, fv in f.fields.items():
            num = float(fv.value) if isinstance(fv.value, int | float) and not isinstance(fv.value, bool) else None
            rows["fact"].append({
                "fact_id": f.fact_id, "entity": f.entity, "field": fname,
                "value_text": fv.value if isinstance(fv.value, str) else (str(fv.value).lower()
                                                                          if isinstance(fv.value, bool) else None),
                "value_num": num,
                "value_json": json.dumps(fv.value, default=str) if isinstance(fv.value, list | dict) else None,
                "raw": fv.raw, "unit": fv.unit, "status": str(fv.status), "method": fv.method, "notes": fv.notes,
                "link": fv.link, "review": json.dumps(fv.review, default=str) if fv.review else None,
                "applicability": f.applicability})
            for s in fv.sources:
                rows["field_evidence"].append({"fact_id": f.fact_id, "field": fname, "record_id": s.record_id,
                                               "page": s.page, "element_ids": s.element_ids, "raw": s.raw})
    for table, (entity, cols) in TYPED.items():
        for f in facts.of(entity):
            row = _typed(f, cols)
            if table == "risk":
                row["rating_mismatch"] = "rating_mismatch" in f.flags
                rows["risk_tmd"] += [{"fact_id": f.fact_id, "tmd_id": t} for t in f.links.get("tmd", [])]
            if table == "section":
                row.update(applicability=f.applicability, pages=f.pages())
            if table == "figure":
                row.update(pages=f.pages(), tmd_ids=f.links.get("tmd", []))
            rows[table].append(row)
    for f in facts.of("speed_limit"):
        hours = f.fields.get("hours_by_day_type")
        for h in (hours.value if hours and isinstance(hours.value, list) else []):
            rows["speed_limit_window"].append({"fact_id": f.fact_id, "day_type": h.get("day_type"),
                                               "time_from": h.get("from"), "time_to": h.get("to"), "raw": h.get("raw")})
    for f in [*facts.of("vulnerable_user"), *facts.of("road_aspect"), *facts.of("form_answer")]:
        question = f.fields.get("question")
        for fname, fv in f.fields.items():
            if fname == "question" or fname.endswith("_note"):
                continue
            answer = ("ticked" if fv.value else "not ticked") if isinstance(fv.value, bool) else fv.value
            rows["checkbox"].append({"fact_id": f.fact_id, "field": fname,
                                     "question": question.value if question else fname, "answer": answer,
                                     "status": str(fv.status)})
    for sh in facts.tmd_sheets:
        rows["tmd_sheet"].append({"tmd_id": sh.tmd_id, "sheet": sh.sheet, "title": sh.title, "rev": sh.rev,
                                  "page": sh.page, "record_id": sh.record_id, "aliases": sh.aliases})
        rec = records.records.get(sh.record_id) if records is not None else None
        for i, sym in enumerate((rec.data.get("symbols") or []) if rec else []):
            rp = sym.get("rp")
            rows["tmd_symbol"].append({"tmd_id": sh.tmd_id, "symbol_id": str(sym.get("id") or i),
                                       "kind": sym.get("kind"), "value": None if sym.get("value") is None
                                       else str(sym.get("value")), "unit": sym.get("unit"),
                                       "rp": float(rp) if isinstance(rp, int | float) else None,
                                       "road": sym.get("road"), "text": sym.get("text")})
    for m in facts.mentions:
        rows["tmd_mention"].append({"mention_id": m.mention_id, "text": m.text, "number": m.number, "title": m.title,
                                    "page": m.page, "record_id": m.source.record_id, "mapped_to": m.mapped_to,
                                    "method": m.method, "status": str(m.status), "id_conflict": m.id_conflict})
    for so in facts.signoffs:
        rows["signoff"].append({"role": so.role, "page": so.page, "status": so.status, "name": so.name,
                                "organisation": so.organisation, "email": so.email, "phone": so.phone,
                                "signature": so.signature, "date_text": so.date, "notes": so.notes})
    for c in facts.conflicts:
        rows["conflict"].append({"conflict_id": c.conflict_id, "rule": c.rule, "title": c.title,
                                 "versions": json.dumps(c.versions, default=str), "affected": c.affected,
                                 "review": json.dumps(c.review, default=str) if c.review else None})
    for ch in facts.checks:
        rows["check_result"].append({"rule": ch.rule, "name": ch.name, "outcome": ch.outcome, "detail": ch.detail,
                                     "conflict_id": ch.conflict_id})
    for r in facts.review:
        rows["review_item"].append({"item_id": r.item_id, "kind": r.kind, "ref": r.ref, "reason": r.reason,
                                    "pages": r.pages, "status": r.status})
    cov = facts.coverage
    rows["coverage"].append({"elements": cov.get("elements"), "records": cov.get("records"),
                             "leftover": cov.get("leftover", []),
                             "records_unaccounted": cov.get("records_unaccounted", []),
                             "complete": cov.get("complete")})
    return rows


# column -> sql type for the casts in INSERT (text parameters are cast so times, dates and json land typed)
_COLUMN_TYPES: dict[str, str] = {"versions": "jsonb", "value_json": "jsonb", "review": "jsonb",
                                 "pages": "int[]", "time_from": "time", "time_to": "time", "rp": "float",
                                 "value_num": "float", "id_conflict": "bool", "rating_mismatch": "bool",
                                 "complete": "bool"}


def _cast(table: str, col: str) -> str:
    typed = TYPED.get(table)
    sql = typed[1].get(col) if typed else None
    sql = sql or _COLUMN_TYPES.get(col)
    return f"::{CAST[sql]}" if sql in ("time", "date", "jsonb") else ""


def connect(dsn: str | None = None) -> Any:
    dsn = dsn or os.environ.get(DSN_ENV)
    if not dsn:
        raise DbError(f"no database configured: set {DSN_ENV}")
    try:
        import psycopg
    except ImportError as e:
        raise DbError("psycopg is not installed: install the 'postgres' extra") from e
    return psycopg.connect(dsn)


def migrate(conn: Any) -> int:
    with conn.transaction():
        conn.execute(schema_sql())
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    return int(row[0])


def write(conn: Any, doc_id: str, rows: dict[str, list[dict[str, Any]]], access_groups: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.transaction():
        conn.execute("DELETE FROM tmp_document WHERE doc_id = %s", (doc_id,))
        for table, items in rows.items():          # tmp_document is first: every other table references it
            for row in items:
                cols = ["doc_id", "access_groups", *row]
                marks = ", ".join(["%s", "%s"] + [f"%s{_cast(table, c)}" for c in row])
                conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                             (doc_id, access_groups, *row.values()))
            counts[table] = len(items)
    return counts


def write_doc_dir(doc_dir: str | Path, dsn: str | None = None, access_groups: list[str] | None = None) -> dict[str, int]:
    d = Path(doc_dir)
    facts = Facts.load(d / "facts.json")
    records = RecordStore.load(d / "records.json") if (d / "records.json").exists() else None
    with connect(dsn) as conn:
        migrate(conn)
        return write(conn, facts.doc_id, build_rows(facts, records), access_groups or [])
