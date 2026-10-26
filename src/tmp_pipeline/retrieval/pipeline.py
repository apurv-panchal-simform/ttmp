"""One question in, one checked text answer out. The flow of a plain RAG:

1. the TMP is the one the user chose from the list they may read (auth decides the list);
2. the question is searched in that TMP's chunks (hybrid search);
3. the model (Q3) writes the answer from that context, or first calls code tools (calculate, time_check, find_tmps,
   compare_versions, get_facts, search_chunks) whose results join the context;
4. code checks the answer (numbers, pages, statuses, conflicts); a number no tool gave goes back to the model once
   with a tool hint, then one repair (Q4), else the code-built safe answer.

``Retriever.ask`` returns the answer with everything the question log needs: the TMP, the chunks, latency per step,
context size, the check result and whether repair was used.
"""
from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .. import logging_ as lg
from .. import tracing
from ..config import Config
from ..llm import ModelClient
from .answer import ASK_BACK, Answered, clean, fixed, repair, role_style, safe_answer, write
from .check import check_answer
from .context import Context, add_tool_results, assemble, library_context
from .search import ChunkSearch, SearchRequest
from .session import Resolution, Session, choose_tmp
from .store import DocInfo, FactsStore, User
from .timecheck import load_calendar
from .tools import ToolEnv
from .words import cited_pages


@dataclass
class AnswerResult:
    question: str
    answer: str
    kind: str                           # answer | ask_back | no_access
    method: str = ""                    # Q3 | Q4 | safe (no model) | safe (check failed) | fixed
    doc_id: str | None = None
    tmp_ref: str | None = None
    resolved_via: str = ""
    candidates: list[str] = field(default_factory=list)        # ask-back options (doc ids)
    context: Context | None = None
    problems: list[str] = field(default_factory=list)          # left after the last attempt
    first_problems: list[str] = field(default_factory=list)    # found in the first answer
    repaired: bool = False
    latency_ms: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0                       # live model calls made for this question (cached replies are free)
    tokens: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)       # tool, args, result, ms of each code tool the model called

    @property
    def cited_pages(self) -> list[int]:
        return sorted(set(cited_pages(self.answer)))


@dataclass
class Retriever:
    store: FactsStore
    search: ChunkSearch
    cfg: Config
    model: ModelClient | None = None
    clock: Callable[[], dt.datetime] | None = None

    def now(self) -> dt.datetime:
        return self.clock() if self.clock else dt.datetime.now(ZoneInfo(self.cfg.retrieval.timezone))

    def ask(self, question: str, session: Session | None = None, user: User | None = None) -> AnswerResult:
        """One traced question. Question and answer text are recorded only with TMP_TRACE_CONTENT=1."""
        session = session if session is not None else Session()
        role = (user.role if user else "") or self.cfg.retrieval.default_role
        with tracing.span("tmp.ask", "chain", **{"langfuse.trace.name": "tmp.ask",
                                                  "langfuse.session.id": session.session_id,
                                                  "tmp.role": role, "tmp.session_tmp": session.tmp_ref}) as sp, \
                tracing.cost_scope() as cost:
            sp.input(question)
            res = self._ask(question, session, user or User())
            res.cost_usd = round(cost.usd, 6)
            res.tokens = {"input": cost.input_tokens, "output": cost.output_tokens, "calls": cost.calls}
            sp.set(**{"tmp.cost_usd": res.cost_usd, "tmp.model_calls": cost.calls,
                      "tmp.tokens.input": cost.input_tokens, "tmp.tokens.output": cost.output_tokens,
                      "tmp.unpriced_calls": cost.unpriced, "tmp.kind": res.kind, "tmp.method": res.method,
                      "tmp.doc_id": res.doc_id, "tmp.tmp_ref": res.tmp_ref, "tmp.resolved_via": res.resolved_via,
                      "tmp.context_tokens": res.context.tokens if res.context else 0,
                      "tmp.chunks": len(res.context.hits) if res.context else 0,
                      "tmp.check_problems": len(res.problems), "tmp.first_check_problems": len(res.first_problems),
                      "tmp.repaired": res.repaired, **{f"tmp.latency_ms.{k}": v for k, v in res.latency_ms.items()},
                      "langfuse.trace.tags": [res.kind], "langfuse.trace.metadata.method": res.method,
                      "langfuse.trace.metadata.tmp_ref": res.tmp_ref, "langfuse.trace.metadata.role": role,
                      "langfuse.trace.metadata.cost_usd": f"{res.cost_usd:.6f}",
                      "langfuse.trace.metadata.tokens": f"{cost.input_tokens} in / {cost.output_tokens} out"})
            if res.problems:
                sp.set(**{"tmp.check": res.problems[:10], "langfuse.observation.level": "WARNING",
                          "langfuse.observation.status_message": "; ".join(res.problems)[:300]})
            sp.output(res.answer)
            return res

    def _ask(self, question: str, session: Session, user: User) -> AnswerResult:
        lat: dict[str, float] = {}
        t0 = t = time.perf_counter()

        def lap(name: str) -> None:
            nonlocal t
            now = time.perf_counter()
            lat[name], t = round((now - t) * 1000, 1), now

        now = self.now()
        now_nz = f"{now:%Y-%m-%d %A %H:%M}"
        res: Resolution = choose_tmp(self.store, user, session)
        if res.doc is None:                         # no TMP to answer from
            if res.status == "several" and self.model is not None and self.cfg.retrieval.tools:
                done = self._library(question, session, user, res, now_nz, lat)
                if done is not None:
                    return self._done(done, t0)
            kind = "ask_back" if res.status == "several" else res.status
            text = fixed(self.cfg, kind, n=len(res.candidates), options="; ".join(d.label() for d in res.candidates))
            return self._done(AnswerResult(question, text, kind, "fixed", resolved_via=res.via, latency_ms=lat,
                                           candidates=[d.doc_id for d in res.candidates]), t0)
        doc = res.doc
        session.use(doc)

        query = " ".join([*[x[3:] for x in session.turns[-2:-1] if x.startswith("Q: ")], question])  # + the last question
        with tracing.span("search_chunks", "retriever", **{"tmp.doc_id": doc.doc_id}) as sp:
            sp.input(query)
            hits = self.search.search(SearchRequest(doc.doc_id, query, self.cfg.retrieval.search_k, include_not_applicable=True), user)
            sp.set(**{"tmp.results": len(hits)})
            sp.output([{"chunk": h.chunk.chunk_id, "score": round(h.score, 4), "via": h.via} for h in hits])
        lap("retrieve")
        ctx = assemble(question, doc, hits, self.cfg.retrieval.context_budget)
        out = AnswerResult(question, "", "answer", doc_id=doc.doc_id, tmp_ref=doc.tmp_ref, resolved_via=res.via,
                           context=ctx, latency_ms=lat)
        lap("context")

        style = role_style(self.cfg, user.role or self.cfg.retrieval.default_role)
        env = self._env(user, doc, now, {h.chunk.chunk_id for h in hits})
        done = write(self.model, ctx, style, now_nz, session.turns, env, self.cfg)
        answer = done.answer
        calls = list(done.calls)
        add_tool_results(ctx, [c.block() for c in calls])
        lap("answer")
        if answer is not None and not calls and self.cfg.retrieval.tools and self.model is not None:
            wrong = [p for p in check_answer(answer, ctx) if p.startswith("number ")]
            if wrong:                       # a number nothing in the context gives: once more, with the tools in mind
                hint = ("These numbers are not in the context: " + "; ".join(p.split(" not in")[0][7:] for p in wrong) +
                        ". Call calculate or time_check for them, or leave them out.")
                again = write(self.model, ctx, style, now_nz, session.turns, env, self.cfg, hint=hint)
                if again.answer is not None:
                    answer, calls = again.answer, list(again.calls)
                    add_tool_results(ctx, [c.block() for c in calls])
                lap("tool retry")
        out.tool_calls = [c.as_dict() for c in calls]
        if answer is None:
            out.answer, out.method = clean(safe_answer(ctx, self.cfg)), "safe (no model)"
            out.problems = check_answer(out.answer, ctx)
        else:
            out.answer, out.method = answer, "Q3"
            out.problems = out.first_problems = check_answer(answer, ctx)
            if out.problems:
                again = repair(self.model, out.problems, answer, ctx, now_nz)
                out.repaired = True
                if again and not (left := check_answer(again, ctx)):
                    out.answer, out.method, out.problems = again, "Q4", left
                else:
                    out.answer, out.method = clean(safe_answer(ctx, self.cfg)), "safe (check failed)"
                    out.problems = check_answer(out.answer, ctx)
        lap("check")
        if out.problems:
            lg.log_warning("ask", "answer_check_failed", problems=out.problems, method=out.method)
        session.remember(question, out.answer)
        return self._done(out, t0)

    def _env(self, user: User, doc: DocInfo | None, now: dt.datetime, seen: set[str]) -> ToolEnv:
        return ToolEnv(self.store, self.search, self.cfg, user, doc, now, load_calendar(self.cfg.retrieval.calendar), seen)

    def _library(self, question: str, session: Session, user: User, res: Resolution, now_nz: str,
                 lat: dict[str, float]) -> AnswerResult | None:
        """Several TMPs can be read and none is chosen: the model gets the list and the tools (find_tmps, get_facts and
        search_chunks with a TMP), and either answers across them or says which TMP it needs. None: ask back."""
        ctx = library_context(question, res.candidates)
        env = self._env(user, None, self.now(), set())
        done: Answered = write(self.model, ctx, role_style(self.cfg, user.role or self.cfg.retrieval.default_role), now_nz,
                               session.turns, env, self.cfg, library=True)
        if done.answer is None or done.answer == ASK_BACK:
            return None
        add_tool_results(ctx, [c.block() for c in done.calls])
        problems = check_answer(done.answer, ctx)
        if problems:
            return None
        session.remember(question, done.answer)
        return AnswerResult(question, done.answer, "answer", "Q3", resolved_via="all TMPs", context=ctx, latency_ms=lat,
                            candidates=[d.doc_id for d in res.candidates], tool_calls=[c.as_dict() for c in done.calls])

    def _done(self, out: AnswerResult, t0: float) -> AnswerResult:
        out.latency_ms["total"] = round((time.perf_counter() - t0) * 1000, 1)
        lg.log_event("ask", "answered", question=out.question, kind=out.kind, method=out.method, doc_id=out.doc_id,
                     chunks=out.context.chunk_ids if out.context else [], latency_ms=out.latency_ms,
                     context_tokens=out.context.tokens if out.context else 0, check=out.problems,
                     first_check=out.first_problems, repaired=out.repaired)
        return out
