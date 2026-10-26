"""Assemble the context the answer is written from: the TMP line and the retrieved chunks, best first. Every chunk carries
its status, scope and source; chunk text already states unconfirmed values and conflicting versions in words. The
budget is in tokens; chunks are cut from the bottom of the ranking first."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..ingestion.chunking.tokens import count
from .search import Hit
from .session import tmp_key
from .store import DocInfo
from .words import cited_pages


def show(value: Any) -> str:
    if isinstance(value, bool):
        return "ticked" if value else "not ticked"
    if value is None:
        return "blank"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list | dict):
        if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
            return ", ".join(str(v) for v in value)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _chunk_block(i: int, h: Hit) -> str:
    c, md = h.chunk, h.chunk.metadata
    scope = md.get("scope", "site_specific")
    tags = [f"status: {md.get('status', 'verified')}",
            "scope: generic (the contractor's general standard, not specific to this site)" if scope == "generic"
            else f"scope: {scope}"]
    if md.get("applicability") == "not_applicable":
        tags.append("struck through (not applicable)")
    if md.get("conflict_ids"):
        tags.append(f"conflicts: {', '.join(md['conflict_ids'])}")
    return f"[{i}] {c.header.split(chr(10), 1)[0]} [{' · '.join(tags)}]\n{c.content}"


@dataclass
class Context:
    text: str                                              # what Q3/Q4 see after their QUESTION line
    tokens: int
    question: str = ""                                     # its numbers are not invented
    pages: set[int] = field(default_factory=set)
    hits: list[Hit] = field(default_factory=list)          # the chunks that fitted the budget, best first
    dropped: list[str] = field(default_factory=list)       # chunk ids cut to fit the budget
    tools: list[str] = field(default_factory=list)         # tool calls and results as the text shows them

    @property
    def chunk_ids(self) -> list[str]:
        return [h.chunk.chunk_id for h in self.hits]

    @property
    def generic(self) -> list[str]:                        # chunk text from the contractor's general standard
        return [h.chunk.content for h in self.hits if h.chunk.metadata.get("scope") == "generic"]

    @property
    def struck(self) -> list[str]:                         # chunk text from struck-through sections
        return [h.chunk.content for h in self.hits if h.chunk.metadata.get("applicability") == "not_applicable"]

    @property
    def empty(self) -> bool:                               # nothing at all answers the question
        return not self.hits and not self.tools


def assemble(question: str, doc: DocInfo, hits: list[Hit], budget: int) -> Context:
    head = (f"TMP: {doc.tmp_ref} · {doc.site_address or ', '.join(doc.roads[:1])} · version V{doc.version} · "
            f"approval: {doc.approval_status or 'unknown'}")
    kept, dropped = list(hits), []
    while True:
        blocks = "\n\n".join(_chunk_block(i, h) for i, h in enumerate(kept, 1)) or "(none)"
        text = f"{head}\nCHUNKS\n{blocks}"
        tokens = count(text)
        if tokens <= budget or not kept:
            break
        dropped.append(kept.pop().chunk.chunk_id)
    pages: set[int] = set()
    for h in kept:
        md = h.chunk.metadata
        pages |= set(md.get("pages") or ([md["page"]] if md.get("page") else []))
    pages |= set(cited_pages(text))                        # pages the context text itself names
    return Context(text, tokens, question, {p for p in pages if isinstance(p, int)}, kept, dropped)


def add_tool_results(ctx: Context, blocks: list[str]) -> Context:
    """The tools' results go into the context text, after the chunks and never cut, so the answer check accepts the
    numbers they computed and the pages they cite exactly as it does those of a chunk."""
    blocks = [b for b in blocks if b]
    if not blocks:
        return ctx
    ctx.tools = [*ctx.tools, *blocks]
    ctx.text = ctx.text.split("\nTOOL RESULTS\n", 1)[0] + "\nTOOL RESULTS\n" + "\n\n".join(ctx.tools)
    ctx.tokens = count(ctx.text)
    ctx.pages |= {p for p in cited_pages(ctx.text) if isinstance(p, int)}
    return ctx


def library_context(question: str, docs: list[DocInfo]) -> Context:
    """No TMP is chosen and several can be read: the context lists them, and the tools do the rest."""
    rows = "\n".join(f"- {tmp_key(d)} · {d.label()} · approval: {d.approval_status or 'unknown'}" for d in docs)
    text = f"No TMP is chosen. The TMPs this user can read:\n{rows}"
    return Context(text, count(text), question)
