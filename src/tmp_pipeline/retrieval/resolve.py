"""Step 1, resolve the TMP (plan 5.3): session, then TMP reference, then address or road, then a doc_id filter.

One match gives a hard ``doc_id`` for every tool; several give an ask-back (the only time the chatbot asks); none is
fine for across-TMP and general questions. The newest version is used unless the question names one.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from ..config import Config
from ..facts.common import norm_name
from .details import norm_ref, tmp_refs
from .store import DocInfo, FactsStore, User, latest

_VERSION = re.compile(r"(?i)\bV(\d{1,2})\b|\bversion\s+(\d{1,2})\b")


@dataclass
class Session:
    """What the conversation remembers (plan 5.14): the current TMP and the last details; switching TMP clears them."""
    doc_id: str | None = None
    tmp_ref: str | None = None
    details: dict[str, str] = field(default_factory=dict)
    types: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)    # groups a conversation in traces

    def use(self, doc: DocInfo) -> None:
        if doc.doc_id != self.doc_id:
            self.details, self.types, self.entities = {}, [], []
        self.doc_id, self.tmp_ref = doc.doc_id, doc.tmp_ref

    def remember(self, question: str, answer: str, types: list[str], details: dict[str, str],
                 entities: list[str]) -> None:
        self.types, self.entities = list(types), list(entities)
        self.details = {**self.details, **{k: v for k, v in details.items() if k != "tmp_ref"}}
        self.turns = (self.turns + [f"Q: {question}", f"A: {answer[:300]}"])[-6:]


@dataclass
class Resolution:
    status: str                         # one | several | none | no_match | no_access
    doc: DocInfo | None = None
    candidates: list[DocInfo] = field(default_factory=list)
    via: str = ""                       # session | tmp_ref | alias | address | road | suburb


def _version(text: str) -> int | None:
    m = _VERSION.search(text)
    return int(m.group(1) or m.group(2)) if m else None


def _pick(docs: list[DocInfo], version: int | None) -> list[DocInfo]:
    if version is not None:
        exact = [d for d in docs if d.version == version]
        if exact:
            return latest(exact)
    return latest(docs)


def resolve_tmp(store: FactsStore, user: User, cfg: Config, question: str, details: dict[str, str],
                session: Session | None) -> Resolution:
    docs = store.docs(user)
    version = _version(question)
    refs = tmp_refs(question, cfg.retrieval) or ([details["tmp_ref"]] if details.get("tmp_ref") else [])
    if refs:
        want = {norm_ref(r) for r in refs}
        hits = [d for d in docs if norm_ref(d.tmp_ref) in want]
        via = "tmp_ref"
        if not hits:
            hits, via = [d for d in docs if want & {norm_ref(a) for a in d.aliases}], "alias"
        if not hits:
            known = getattr(store, "known_refs", None)
            if known is not None and want & known():
                return Resolution("no_access")
            return Resolution("no_match")
        picked = _pick(hits, version)
        return Resolution("one", picked[0], picked, via) if len({d.tmp_ref for d in picked}) == 1 else \
            Resolution("several", None, picked, via)

    q = f" {norm_name(question, cfg.facts)} "
    house = {m.group(1) for m in re.finditer(r"\b(\d{1,4})[a-z]?\s+[a-z]", q)}
    scored: list[tuple[int, str, DocInfo]] = []
    for d in latest(docs):
        score, via = 0, ""
        site = norm_name(d.site_address or "", cfg.facts)
        for road in d.roads:
            r = norm_name(road, cfg.facts)
            if r and f" {r} " in q:
                score, via = max(score, 2), "road"
                if site and r in site and any(f" {h} {r}" in f" {site}" for h in house):
                    score, via = 3, "address"
            elif r:                              # "Sunrise" alone, without its suffix
                stem = cfg.retrieval.road_suffix.sub("", r).strip()
                if len(stem) > 3 and f" {stem} " in q and score < 1:
                    score, via = 1, "road"
        for sub in d.suburbs + ([d.suburb] if d.suburb else []):
            s = norm_name(sub, cfg.facts)
            if s and f" {s} " in q and score < 1:
                score, via = 1, "suburb"
        if score:
            scored.append((score, via, d))
    if scored:
        best = max(s for s, _, _ in scored)
        cands = [(v, d) for s, v, d in scored if s == best]
        if session and session.doc_id and any(d.doc_id == session.doc_id for _, d in cands):
            doc = next(d for _, d in cands if d.doc_id == session.doc_id)
            return Resolution("one", doc, [doc], "session")
        if len(cands) == 1:
            return Resolution("one", cands[0][1], [cands[0][1]], cands[0][0])
        return Resolution("several", None, [d for _, d in cands], cands[0][0])

    if session and session.doc_id:
        current = next((d for d in docs if d.doc_id == session.doc_id), None)
        if current is not None:
            if version is not None and version != current.version:
                other = _pick([d for d in docs if d.tmp_ref == current.tmp_ref], version)
                if other and other[0].version == version:
                    return Resolution("one", other[0], other, "session")
            return Resolution("one", current, [current], "session")
        return Resolution("no_access")
    return Resolution("none")
