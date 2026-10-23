"""Records: interpreted facts produced by handlers, kept apart from the elements they were read from.

An element is extracted structure (a table, a text block); a record says what it means (a road, a risk row, a
ticked checkbox). Every value carries its status and the elements it came from, so lineage survives all the
way to chunks and answers. Records are stamped with the handler, config, prompt and model versions that
produced them, so a change to any of those can invalidate exactly the records it affects (plan section 6.2).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, Element, SchemaError, atomic_write_text
from .vocab import Flag

log = logging.getLogger(__name__)


class Status(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"      # for example a struck-through section
    CONFLICT = "conflict"                  # the document contradicts itself; every version is kept (Phase 4)
    REVIEWED = "reviewed"                  # a person confirmed or corrected the value (Phase 4)


@dataclass
class FieldValue:
    value: Any                              # typed, clean value (0.41, "2026-09-28", True)
    raw: str | None = None                  # the text exactly as read ("RP 0.410")
    unit: str | None = None
    status: Status = Status.VERIFIED
    method: str = "code"                    # code | pattern | catalog | X2 | X4 | X5 | X6
    quote: str | None = None                # sentence the value was taken from, when a model read it
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = Status(self.status)


@dataclass
class Evidence:
    element_id: str
    fingerprint: str | None = None
    bbox_pt: list[float] | None = None
    quote: str | None = None

    @classmethod
    def of(cls, element: Element, quote: str | None = None) -> Evidence:
        return cls(element.element_id, element.fingerprint(), list(element.bbox_pt), quote)


@dataclass
class Stamp:
    handler: str
    handler_version: str
    config_hash: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    model: str = "none"
    catalog_version: str = "0.0.0"


@dataclass
class Record:
    kind: str                               # road | risk_cell | risk_row | checkbox | section | numeric | ...
    page: int
    produced_by: Stamp
    fields: dict[str, FieldValue] = field(default_factory=dict)
    entity: str | None = None               # catalog entity or section type
    evidence: list[Evidence] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)     # ids of records this one was built from
    flags: list[Flag] = field(default_factory=list)
    status: Status = Status.VERIFIED
    applicability: str = "active"           # active | not_applicable
    data: dict = field(default_factory=dict)  # structured payload that is not a field (section text, cell grid)
    record_id: str = ""

    def __post_init__(self) -> None:
        self.status = Status(self.status)
        self.flags = [Flag(f) for f in self.flags]

    def element_ids(self) -> list[str]:
        return [e.element_id for e in self.evidence]

    def content_hash(self) -> str:
        """Hash of what the record says, ignoring its id and stamp: equal across re-runs with unchanged output."""
        body = {"kind": self.kind, "entity": self.entity, "page": self.page,
                "fields": {k: [v.value, v.raw, v.unit, str(v.status)] for k, v in sorted(self.fields.items())},
                "data": self.data}
        return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def _from_dict(raw: dict) -> Record:
    known = {f.name for f in fields(Record)}
    extra = sorted(set(raw) - known)
    if extra:
        log.warning("ignoring unknown record field(s) %s (written by a newer version?)", ", ".join(extra))
    raw = {k: v for k, v in raw.items() if k in known}
    raw["produced_by"] = Stamp(**raw["produced_by"])
    raw["fields"] = {k: FieldValue(**v) for k, v in raw.get("fields", {}).items()}
    raw["evidence"] = [Evidence(**e) for e in raw.get("evidence", [])]
    return Record(**raw)


class RecordStore:
    def __init__(self, doc_id: str = ""):
        self.doc_id = doc_id
        self.records: dict[str, Record] = {}
        self._counters: dict[tuple[int, str], int] = {}
        self._by_element: dict[str, list[str]] = defaultdict(list)
        self._by_fingerprint: dict[str, list[str]] = defaultdict(list)

    def add(self, record: Record) -> Record:
        if not record.record_id:
            n = self._counters.get((record.page, record.kind), 0)
            record.record_id = f"r{record.page}:{record.kind}:{n}"
            self._counters[(record.page, record.kind)] = n + 1
        if record.record_id in self.records:
            raise ValueError(f"duplicate record id {record.record_id!r}")
        self.records[record.record_id] = record
        for ev in record.evidence:
            self._by_element[ev.element_id].append(record.record_id)
            if ev.fingerprint:
                self._by_fingerprint[ev.fingerprint].append(record.record_id)
        return record

    def records_for(self, element_id: str) -> list[Record]:
        return [self.records[i] for i in self._by_element.get(element_id, [])]

    def by_fingerprint(self, fingerprint: str) -> list[Record]:
        return [self.records[i] for i in self._by_fingerprint.get(fingerprint, [])]

    def of_kind(self, *kinds: str, page: int | None = None) -> list[Record]:
        return [r for r in self.records.values() if r.kind in kinds and (page is None or r.page == page)]

    def __len__(self) -> int:
        return len(self.records)

    def dump(self, path: str | Path) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "doc_id": self.doc_id,
                   "records": [asdict(r) for r in self.records.values()]}
        atomic_write_text(path, json.dumps(payload, indent=1, ensure_ascii=False, default=str))

    @classmethod
    def load(cls, path: str | Path) -> RecordStore:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version", 0) > SCHEMA_VERSION:
            raise SchemaError(f"{path}: schema_version {data['schema_version']} is newer than supported {SCHEMA_VERSION}")
        store = cls(data["doc_id"])
        for raw in data.get("records", []):
            rec = _from_dict(raw)
            store.add(rec)
            parts = rec.record_id.split(":")
            if len(parts) == 3 and parts[2].isdigit() and parts[0].startswith("r") and parts[0][1:].isdigit():
                key = (int(parts[0][1:]), parts[1])
                store._counters[key] = max(store._counters.get(key, 0), int(parts[2]) + 1)
        return store
