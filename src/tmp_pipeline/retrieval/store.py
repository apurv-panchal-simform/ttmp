"""Where retrieval reads from (plan 5.6, 5.14): the facts of each TMP and its chunks, filtered by the user's access.

``FactsStore`` is the interface the tools use. ``LocalStore`` reads the run folders (``out/<doc>/facts.json`` and
``chunks.jsonl``); the optional ``db`` package provides a PostgreSQL store with the same interface. Every read takes
the ``User`` so access rules apply inside the store, never in a prompt.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Protocol

from .. import logging_ as lg
from ..errors import PipelineError
from ..ingestion.chunking.model import ChunkSet
from ..ingestion.facts.model import Facts
from ..records import Status


class NoAccessError(PipelineError):
    """The user may not read this TMP (the answer never says more than that)."""


class UnknownDocError(PipelineError):
    """No TMP with this doc_id is stored."""


@dataclass(frozen=True)
class User:
    groups: tuple[str, ...] = ()
    role: str = ""                      # answer style (retrieval.role_styles); "" = the default role

    def may_read(self, access_groups: list[str] | tuple[str, ...]) -> bool:
        """A TMP with no access groups is open to every signed-in user; otherwise the groups must overlap."""
        return not access_groups or bool(set(access_groups) & set(self.groups))


@dataclass
class DocInfo:
    """What resolving and listing TMPs needs, without loading every fact."""
    doc_id: str
    tmp_ref: str
    aliases: list[str] = field(default_factory=list)
    version: int = 1                    # TMP version (V1, V2 ...); 1 when the document does not say
    site_address: str | None = None
    suburb: str | None = None
    suburbs: list[str] = field(default_factory=list)
    roads: list[str] = field(default_factory=list)
    work_start: str | None = None
    work_end: str | None = None
    approval_status: str | None = None
    access_groups: list[str] = field(default_factory=list)
    organisations: list[str] = field(default_factory=list)   # contacts and sign-off organisations
    has_conflict: bool = False
    written: float = 0.0                # tie-breaker between two files of the same version

    def label(self) -> str:
        where = self.site_address or ", ".join(self.roads[:1] + ([self.suburb] if self.suburb else [])) or "?"
        dates = f", {self.work_start} to {self.work_end}" if self.work_start and self.work_end else ""
        return f"{where} ({self.tmp_ref}{dates})"


class FactsStore(Protocol):
    def docs(self, user: User) -> list[DocInfo]: ...
    def facts(self, doc_id: str, user: User) -> Facts: ...
    def chunks(self, doc_id: str, user: User) -> ChunkSet | None: ...


def version_number(text: object) -> int:
    """"V2", "v2" or "2" -> 2; anything else -> 1."""
    s = str(text or "").strip()
    if s[:1] in ("V", "v"):
        s = s[1:]
    return int(s) if s.isascii() and s.isdigit() else 1


def tmp_reference(facts: Facts) -> tuple[str | None, list[str]]:
    """The TMP reference and its other spellings; with a conflict the most frequent spelling leads."""
    doc = next(iter(facts.of("tmp_document")), None)
    ref = doc.fields.get("tmp_reference") if doc else None
    if ref is None:
        return None, []
    if ref.status == Status.CONFLICT and ref.versions:
        spellings = [str(v["value"]) for v in ref.versions if v.get("value")]
        return (spellings[0] if spellings else None), spellings[1:]
    others = [str(v["value"]) for v in ref.versions[1:] if v.get("value")] if ref.versions else []
    return (str(ref.value) if ref.value else None), others


def _first(facts: Facts, entity: str, fname: str) -> object:
    for f in facts.of(entity):
        fv = f.fields.get(fname)
        if fv is not None and fv.value not in (None, "") and fv.status != Status.CONFLICT:
            return fv.value
    return None


def doc_info(facts: Facts, card: dict | None = None, version: object = None, access_groups: list[str] | None = None,
             written: float = 0.0) -> DocInfo:
    """``card`` is the doc_card chunk's metadata when chunks exist (it holds the site address the facts don't)."""
    card = card or {}
    ref, aliases = tmp_reference(facts)
    roads = [str(f.fields["road_name"].value) for f in facts.of("road")
             if "road_name" in f.fields and f.fields["road_name"].value]
    suburbs = list(dict.fromkeys(str(f.fields["suburb"].value) for f in facts.of("road")
                                 if "suburb" in f.fields and f.fields["suburb"].value))
    start, end = _first(facts, "work_period", "start_date"), _first(facts, "work_period", "end_date")
    orgs = [str(f.fields["organisation"].value) for f in facts.of("contact")
            if "organisation" in f.fields and f.fields["organisation"].value]
    orgs += [s.organisation for s in facts.signoffs if s.organisation]
    return DocInfo(
        doc_id=facts.doc_id, tmp_ref=ref or card.get("tmp_ref") or facts.doc_id.split(":")[-1][:12],
        aliases=aliases or list(card.get("tmp_ref_aliases") or []), version=version_number(version),
        site_address=card.get("site_address"), suburb=card.get("suburb") or (suburbs[0] if suburbs else None),
        suburbs=suburbs, roads=roads,
        work_start=str(start) if start else card.get("work_start"), work_end=str(end) if end else card.get("work_end"),
        approval_status=facts.approval.status if facts.approval else None,
        organisations=list(dict.fromkeys(o for o in orgs if o and o != "None")), has_conflict=bool(facts.conflicts),
        access_groups=list(access_groups if access_groups is not None else card.get("access_groups") or []),
        written=written)


class LocalStore:
    """The run folders under one output root (``out/``): one folder per document."""

    def __init__(self, root: str | Path, chunks_from: Callable[[str], ChunkSet | None] | None = None):
        """``chunks_from`` reads a document's chunks from elsewhere (for example Cosmos DB) instead of chunks.jsonl."""
        self.root = Path(root)
        self.chunks_from = chunks_from
        self._facts: dict[str, Facts] = {}
        self._chunks: dict[str, ChunkSet | None] = {}

    def _dirs(self) -> list[Path]:
        if (self.root / "facts.json").exists():                 # a single document folder
            return [self.root]
        return sorted(p for p in self.root.iterdir() if (p / "facts.json").exists()) if self.root.is_dir() else []

    @cached_property
    def _index(self) -> dict[str, tuple[Path, DocInfo]]:
        out: dict[str, tuple[Path, DocInfo]] = {}
        for d in self._dirs():
            facts = Facts.load(d / "facts.json")
            if self.chunks_from is not None:
                chunks = self.chunks_from(facts.doc_id) or None
            else:
                chunks = ChunkSet.load_jsonl(d / "chunks.jsonl") if (d / "chunks.jsonl").exists() else None
            card = next((c.metadata for c in chunks.of("doc_card")), None) if chunks else None
            version = None
            if (d / "elements.json").exists():
                version = json.loads((d / "elements.json").read_text(encoding="utf-8")).get("metadata", {}).get("version")
            info = doc_info(facts, card, version, written=(d / "facts.json").stat().st_mtime)
            self._facts[facts.doc_id], self._chunks[facts.doc_id] = facts, chunks
            out[facts.doc_id] = (d, info)
        lg.log_event("retrieval", "store_loaded", root=str(self.root), docs=len(out))
        return out

    def _info(self, doc_id: str, user: User) -> DocInfo:
        hit = self._index.get(doc_id)
        if hit is None:
            raise UnknownDocError(f"no stored TMP {doc_id}")
        if not user.may_read(hit[1].access_groups):
            raise NoAccessError(doc_id)
        return hit[1]

    def docs(self, user: User) -> list[DocInfo]:
        return [info for _, info in self._index.values() if user.may_read(info.access_groups)]

    def facts(self, doc_id: str, user: User) -> Facts:
        self._info(doc_id, user)
        return self._facts[doc_id]

    def chunks(self, doc_id: str, user: User) -> ChunkSet | None:
        self._info(doc_id, user)
        cs = self._chunks.get(doc_id)
        if cs is None:
            return None
        return ChunkSet(cs.doc_id, cs.versions, cs.facts_revision,
                        [c for c in cs.chunks if user.may_read(c.metadata.get("access_groups") or [])])

    def folder(self, doc_id: str) -> Path | None:
        hit = self._index.get(doc_id)
        return hit[0] if hit else None


def latest(docs: list[DocInfo]) -> list[DocInfo]:
    """One document per TMP reference: the newest version (plan 1.1: a newer version supersedes the older one)."""
    best: dict[str, DocInfo] = {}
    for d in docs:
        cur = best.get(d.tmp_ref)
        if cur is None or (d.version, d.written) > (cur.version, cur.written):
            best[d.tmp_ref] = d
    return list(best.values())
