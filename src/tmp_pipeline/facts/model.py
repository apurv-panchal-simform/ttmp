"""The canonical TMP record (plan sections 3.2-3.4): facts merged from handler records, the diagram register,
every diagram mention, sign-offs, approval, conflicts, check results, coverage and the review list.

Facts never replace records. Every value points back to the records, pages and elements it came from
(``Source``), so a reviewer correction or a catalog change can be traced and re-applied without reading the PDF.
"""
from __future__ import annotations

import json
import types
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ..models import SCHEMA_VERSION, SchemaError, atomic_write_text
from ..records import Status


@dataclass
class Source:
    record_id: str                  # "" when the value came from page furniture rather than a record
    page: int
    element_ids: list[str] = field(default_factory=list)
    raw: str | None = None


@dataclass
class FactValue:
    value: Any
    raw: str | None = None
    unit: str | None = None
    status: Status = Status.VERIFIED
    method: str = "code"            # code | catalog | pattern | vector | merge | pointer | X5 | X11 ...
    sources: list[Source] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    versions: list[dict] = field(default_factory=list)   # conflict: every value the document gives, with sources
    link: list[str] = field(default_factory=list)        # resolved pointer: the fact paths it refers to
    review: dict | None = None      # reviewer, at, action, note, original

    def __post_init__(self) -> None:
        self.status = Status(self.status)


@dataclass
class Fact:
    fact_id: str                    # "<entity>:<n>"
    entity: str
    fields: dict[str, FactValue] = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=dict)   # "tmd" -> ["TMD-03"], "section" -> [fact ids]
    applicability: str = "active"
    flags: list[str] = field(default_factory=list)

    def pages(self) -> list[int]:
        return sorted({s.page for s in self.sources})


@dataclass
class TmdSheet:
    tmd_id: str
    sheet: str
    title: str
    page: int
    record_id: str
    rev: str | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass
class TmdMention:
    mention_id: str
    text: str                       # the mention as written ("TMD4: Parking Management")
    number: int | None
    title: str | None
    page: int
    source: Source
    context: str | None = None      # group label or heading path; marks stage lists
    mapped_to: str | None = None    # TMD id after the title-wins rule
    method: str = "none"            # number_and_title | title | number | synonym | X11 | none
    status: Status = Status.VERIFIED
    id_conflict: bool = False
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = Status(self.status)


@dataclass
class Signoff:
    role: str
    page: int
    status: str                     # signed | unsigned | not_applicable
    name: str | None = None
    organisation: str | None = None
    email: str | None = None
    phone: str | None = None
    signature: str | None = None
    date: str | None = None
    sources: list[Source] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Approval:
    status: str                     # signed | partially_signed | unsigned
    required: list[str] = field(default_factory=list)
    signed: list[str] = field(default_factory=list)
    unsigned: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Conflict:
    conflict_id: str                # C-001, numbered in rule order so reruns give the same ids
    rule: int
    title: str
    versions: list[dict] = field(default_factory=list)   # {"value": ..., "pages": [...], "sources": [...], "note"?}
    affected: list[str] = field(default_factory=list)    # fact paths and mention ids the conflict marks
    review: dict | None = None


@dataclass
class CheckResult:
    rule: int
    name: str
    outcome: str                    # pass | conflict | flagged | not_checked
    detail: str = ""
    conflict_id: str | None = None
    items: list[str] = field(default_factory=list)


@dataclass
class ReviewItem:
    item_id: str
    kind: str                       # unverified | conflict | rating_mismatch | leftover | unmatched_mention | unsigned
    ref: str                        # fact path, conflict id, mention id, record id or element id
    reason: str
    pages: list[int] = field(default_factory=list)
    values: list[Any] = field(default_factory=list)
    status: str = "open"            # open | closed


@dataclass
class Facts:
    doc_id: str
    versions: dict[str, str] = field(default_factory=dict)
    config_hash: str = ""
    catalog_version: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    revision: int = 1               # bumped by every applied set of reviewer corrections
    facts: list[Fact] = field(default_factory=list)
    tmd_sheets: list[TmdSheet] = field(default_factory=list)
    mentions: list[TmdMention] = field(default_factory=list)
    signoffs: list[Signoff] = field(default_factory=list)
    approval: Approval | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    other: list[dict] = field(default_factory=list)      # handler records that hold no fact: {record_id, kind, page}
    review: list[ReviewItem] = field(default_factory=list)

    # --- lookups ---------------------------------------------------------------------------------------------
    def of(self, entity: str) -> list[Fact]:
        return [f for f in self.facts if f.entity == entity]

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.fact_id == fact_id), None)

    def value(self, path: str) -> FactValue | None:
        """The value at "<fact_id>.<field>" (fact ids contain a colon, never a dot)."""
        fid, _, fname = path.partition(".")
        f = self.fact(fid)
        return f.fields.get(fname) if f else None

    def add(self, entity: str, **kw: Any) -> Fact:
        n = sum(f.entity == entity for f in self.facts)
        fact = Fact(f"{entity}:{n}", entity, **kw)
        self.facts.append(fact)
        return fact

    def sheet(self, tmd_id: str) -> TmdSheet | None:
        return next((s for s in self.tmd_sheets if s.tmd_id == tmd_id), None)

    # --- persistence -----------------------------------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({"schema_version": SCHEMA_VERSION, **asdict(self)}, indent=1, ensure_ascii=False,
                          default=str)

    def dump(self, path: str | Path) -> None:
        atomic_write_text(path, self.to_json())

    @classmethod
    def load(cls, path: str | Path) -> Facts:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.pop("schema_version", 0) > SCHEMA_VERSION:
            raise SchemaError(f"{path}: facts schema_version is newer than supported {SCHEMA_VERSION}")
        return _build(cls, data)


def review_json(facts: Facts) -> str:
    return json.dumps({"schema_version": SCHEMA_VERSION, "doc_id": facts.doc_id, "revision": facts.revision,
                       "items": [asdict(i) for i in facts.review]}, indent=1, ensure_ascii=False, default=str)


def _build(tp: Any, raw: Any) -> Any:
    """Rebuild nested dataclasses from their ``asdict`` form, following the type hints."""
    if raw is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (types.UnionType, typing.Union):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _build(args[0], raw) if len(args) == 1 else raw
    if origin is list:
        (arg,) = typing.get_args(tp)
        return [_build(arg, v) for v in raw]
    if origin is dict:
        _, vt = typing.get_args(tp)
        return {k: _build(vt, v) for k, v in raw.items()}
    if isinstance(tp, type) and is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        known = {f.name for f in fields(tp)}
        return tp(**{k: _build(hints[k], v) for k, v in raw.items() if k in known})
    return raw
