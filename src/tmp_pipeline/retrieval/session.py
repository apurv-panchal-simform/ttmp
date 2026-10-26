"""The conversation (the TMP the user chose and the last turns) and finding a TMP the user typed or picked."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .store import DocInfo, FactsStore, User, latest
from .words import norm_ref, normalise


@dataclass
class Session:
    """What the conversation remembers: the chosen TMP and the last turns. Switching TMP clears the turns."""
    doc_id: str | None = None
    tmp_ref: str | None = None
    turns: list[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)    # groups a conversation in traces

    def use(self, doc: DocInfo) -> None:
        if doc.doc_id != self.doc_id:
            self.turns = []
        self.doc_id, self.tmp_ref = doc.doc_id, doc.tmp_ref

    def remember(self, question: str, answer: str) -> None:
        self.turns = (self.turns + [f"Q: {question}", f"A: {answer[:300]}"])[-6:]


@dataclass
class Resolution:
    status: str                         # one | several | no_match (find_tmp only) | no_access
    doc: DocInfo | None = None
    candidates: list[DocInfo] = field(default_factory=list)
    via: str = ""                       # session | only TMP | key | reference | address


def tmp_key(d: DocInfo) -> str:
    """How a TMP is named in the list the user picks from: "KTS 26-1581 V2"."""
    return f"{d.tmp_ref} V{d.version}"


def choose_tmp(store: FactsStore, user: User, session: Session) -> Resolution:
    """The TMP a question is answered from: the one the user chose, else the only one they may read."""
    docs = store.docs(user)
    if session.doc_id:
        current = next((d for d in docs if d.doc_id == session.doc_id), None)
        return Resolution("one", current, [current], "session") if current else Resolution("no_access")
    newest = latest(docs)
    if len(newest) == 1:
        return Resolution("one", newest[0], newest, "only TMP")
    return Resolution("several", None, newest, "none chosen") if newest else Resolution("no_access")


def find_tmp(store: FactsStore, user: User, text: str) -> Resolution:
    """A TMP the user typed or picked ("KTS 26-1581", "KTS 26-1581 V1", "55 Sunrise Ave"): by key, reference or alias,
    else by a part of its address, road or suburb. Plain string comparison; used by the command line."""
    docs = store.docs(user)
    exact = {tmp_key(d): d for d in docs}.get(text.strip())
    if exact is not None:
        return Resolution("one", exact, [exact], "key")
    want = norm_ref(text)
    hits = [d for d in latest(docs) if want and want in {norm_ref(d.tmp_ref), *(norm_ref(a) for a in d.aliases)}]
    via = "reference"
    if not hits:
        q = normalise(text)
        hits = [d for d in latest(docs) if q and any(q in normalise(x) for x in (d.site_address or "", *d.roads, *d.suburbs))]
        via = "address"
    if len(hits) == 1:
        return Resolution("one", hits[0], hits, via)
    return Resolution("several", None, hits, via) if hits else Resolution("no_match")
