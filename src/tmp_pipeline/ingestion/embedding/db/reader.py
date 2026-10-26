"""The facts store read back from PostgreSQL (plan 3.4, 5.6): the same ``FactsStore`` the retrieval tools use with
``LocalStore``, rebuilt from the tables ``writer`` fills. Every query carries the user's access groups, so a row the
user may not read never leaves the database (row-level security policies, plan 5.14, are a Phase 8 deployment
step; this filter is the same rule in the query).

Chunks are not in PostgreSQL: pass ``chunks_from`` (for example ``LocalStore(out).chunks``) when the tools should
cite chunk breadcrumbs and read diagram chunks; without it citations fall back to pages.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ....records import Status
from ....retrieval.store import DocInfo, NoAccessError, UnknownDocError, User, doc_info
from ...chunking.model import ChunkSet
from ...facts.model import (
    Approval,
    CheckResult,
    Conflict,
    Fact,
    Facts,
    FactValue,
    Signoff,
    Source,
    TmdMention,
    TmdSheet,
)
from .writer import connect

_ACCESS = "(cardinality(access_groups) = 0 OR access_groups && %s::text[])"


def _value(row: dict) -> Any:
    if row["value_json"] is not None:
        return row["value_json"] if not isinstance(row["value_json"], str) else json.loads(row["value_json"])
    if row["value_num"] is not None:
        n = float(row["value_num"])          # stored as double precision: whole numbers come back as int, except RPs
        return int(n) if n.is_integer() and row["unit"] not in ("km",) else n
    text = row["value_text"]
    if text in ("true", "false"):
        return text == "true"
    return text


class PgFactsStore:
    def __init__(self, dsn: str | None = None, conn: Any = None,
                 chunks_from: Callable[[str, User], ChunkSet | None] | None = None):
        self.conn = conn or connect(dsn)
        self.chunks_from = chunks_from
        self._facts: dict[str, Facts] = {}

    def _rows(self, sql: str, params: tuple) -> list[dict]:
        cur = self.conn.execute(sql, params)
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def _doc_ids(self, user: User) -> list[tuple[str, list[str]]]:
        rows = self._rows(f"SELECT doc_id, access_groups FROM tmp_document WHERE {_ACCESS} ORDER BY doc_id",
                          (list(user.groups),))
        return [(r["doc_id"], list(r["access_groups"] or [])) for r in rows]

    def docs(self, user: User) -> list[DocInfo]:
        out = []
        for doc_id, groups in self._doc_ids(user):
            out.append(doc_info(self.facts(doc_id, user), None, None, groups))
        return out

    def facts(self, doc_id: str, user: User) -> Facts:
        head = self._rows("SELECT * FROM tmp_document WHERE doc_id = %s", (doc_id,))
        if not head:
            raise UnknownDocError(f"no stored TMP {doc_id}")
        if not user.may_read(list(head[0]["access_groups"] or [])):
            raise NoAccessError(doc_id)
        if doc_id in self._facts:
            return self._facts[doc_id]
        h = head[0]
        args = (doc_id, list(user.groups))
        where = f"doc_id = %s AND {_ACCESS}"
        evidence: dict[tuple[str, str], list[Source]] = {}
        for r in self._rows(f"SELECT * FROM field_evidence WHERE {where}", args):
            evidence.setdefault((r["fact_id"], r["field"]), []).append(
                Source(r["record_id"], r["page"], list(r["element_ids"] or []), r["raw"]))
        facts = Facts(doc_id, versions=h["versions"] if isinstance(h["versions"], dict) else json.loads(h["versions"]),
                      config_hash=h["config_hash"] or "", catalog_version=h["catalog_version"] or "",
                      revision=h["revision"])
        by_id: dict[str, Fact] = {}
        for r in self._rows(f"SELECT * FROM fact WHERE {where} ORDER BY entity, fact_id, field", args):
            f = by_id.get(r["fact_id"])
            if f is None:
                f = by_id[r["fact_id"]] = Fact(r["fact_id"], r["entity"], applicability=r["applicability"])
            srcs = evidence.get((r["fact_id"], r["field"]), [])
            f.fields[r["field"]] = FactValue(_value(r), r["raw"], r["unit"], Status(r["status"]), r["method"] or "code",
                                             srcs, list(r["notes"] or []), link=list(r["link"] or []),
                                             review=r["review"] if not isinstance(r["review"], str) else json.loads(r["review"]))
            known = {(s.record_id, s.page) for s in f.sources}
            f.sources += [s for s in srcs if (s.record_id, s.page) not in known]
        for r in self._rows(f"SELECT fact_id, tmd_id FROM risk_tmd WHERE {where}", args):
            if r["fact_id"] in by_id:
                by_id[r["fact_id"]].links.setdefault("tmd", []).append(r["tmd_id"])
        facts.facts = sorted(by_id.values(), key=lambda f: (f.entity, int(f.fact_id.rsplit(":", 1)[-1])
                                                             if f.fact_id.rsplit(":", 1)[-1].isdigit() else 0))
        facts.tmd_sheets = [TmdSheet(r["tmd_id"], r["sheet"], r["title"], r["page"], r["record_id"], r["rev"],
                                     list(r["aliases"] or []))
                            for r in self._rows(f"SELECT * FROM tmd_sheet WHERE {where} ORDER BY tmd_id", args)]
        facts.mentions = [TmdMention(r["mention_id"], r["text"], r["number"], r["title"], r["page"],
                                     Source(r["record_id"] or "", r["page"]), None, r["mapped_to"], r["method"],
                                     Status(r["status"]), r["id_conflict"])
                          for r in self._rows(f"SELECT * FROM tmd_mention WHERE {where} ORDER BY mention_id", args)]
        facts.signoffs = [Signoff(r["role"], r["page"], r["status"], r["name"], r["organisation"], r["email"],
                                  r["phone"], r["signature"], r["date_text"], notes=list(r["notes"] or []))
                          for r in self._rows(f"SELECT * FROM signoff WHERE {where}", args)]
        facts.conflicts = [Conflict(r["conflict_id"], r["rule"], r["title"],
                                    r["versions"] if not isinstance(r["versions"], str) else json.loads(r["versions"]),
                                    list(r["affected"] or []))
                           for r in self._rows(f"SELECT * FROM conflict WHERE {where} ORDER BY conflict_id", args)]
        for c in facts.conflicts:              # a value in conflict keeps its versions in `conflict`, not in `fact`
            for path in c.affected:
                fv = facts.value(path) if "." in path else None
                if fv is not None and not fv.versions:
                    fv.versions = [dict(v) for v in c.versions]
        facts.checks = [CheckResult(r["rule"], r["name"], r["outcome"], r["detail"] or "", r["conflict_id"])
                        for r in self._rows(f"SELECT * FROM check_result WHERE {where} ORDER BY rule", args)]
        if h["approval_status"]:
            so = facts.signoffs
            facts.approval = Approval(h["approval_status"], [s.role for s in so if s.status != "not_applicable"],
                                      [s.role for s in so if s.status == "signed"],
                                      [s.role for s in so if s.status == "unsigned"],
                                      [s.role for s in so if s.status == "not_applicable"])
        self._facts[doc_id] = facts
        return facts

    def chunks(self, doc_id: str, user: User) -> ChunkSet | None:
        self.facts(doc_id, user)                     # the access check
        return self.chunks_from(doc_id, user) if self.chunks_from else None
