"""One question in, one checked text answer out (plan 5.1): resolve, classify, recipes, context, answer, check.

``Retriever.ask`` returns the answer with everything the question log needs (plan 5.14): the standalone question,
types, tools and arguments, latency per step, context size, check result, whether repair was used.
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
from ..model.client import ModelClient
from .answer import clean, fixed, repair, role_style, safe_answer, write
from .check import check_answer
from .classify import NEEDS_TMP, Reading, classify
from .context import Context, assemble
from .hybrid import ChunkSearch
from .recipes import Tools, run_recipes
from .resolve import Resolution, Session, resolve_tmp
from .store import FactsStore, User
from .timecheck import load_calendar


@dataclass
class AnswerResult:
    question: str
    answer: str
    kind: str                           # answer | ask_back | no_match | no_access | no_tmp | not_in_tmp
    method: str = ""                    # Q3 | Q4 | safe (no model) | safe (check failed) | fixed
    doc_id: str | None = None
    tmp_ref: str | None = None
    resolved_via: str = ""
    reading: Reading | None = None
    calls: list[dict] = field(default_factory=list)
    context: Context | None = None
    problems: list[str] = field(default_factory=list)          # left after the last attempt
    first_problems: list[str] = field(default_factory=list)    # found in the first answer
    repaired: bool = False
    candidates: list[str] = field(default_factory=list)        # ask-back options (doc ids)
    latency_ms: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0                       # live model calls made for this question (cached replies are free)
    tokens: dict[str, int] = field(default_factory=dict)

    @property
    def cited_pages(self) -> list[int]:
        from .check import _cited_pages
        return sorted(set(_cited_pages(self.answer)))


@dataclass
class Retriever:
    store: FactsStore
    search: ChunkSearch
    cfg: Config
    model: ModelClient | None = None
    clock: Callable[[], dt.datetime] | None = None

    def now(self) -> dt.datetime:
        return self.clock() if self.clock else dt.datetime.now(ZoneInfo(self.cfg.retrieval.timezone))

    def _example(self, user: User) -> str:
        docs = self.store.docs(user)
        return docs[0].tmp_ref if docs else "the reference on the first page of the TMP"

    def ask(self, question: str, session: Session | None = None, user: User | None = None) -> AnswerResult:
        """One traced question: the span holds the reading, the outcome and per-step timing; tool and model calls
        are its child spans. Question and answer text are recorded only with TMP_TRACE_CONTENT=1."""
        session = session if session is not None else Session()
        role = (user.role if user else "") or self.cfg.retrieval.default_role
        with tracing.span("tmp.ask", "chain", **{"langfuse.trace.name": "tmp.ask",
                                                  "langfuse.session.id": session.session_id,
                                                  "tmp.role": role, "tmp.session_tmp": session.tmp_ref}) as sp, \
                tracing.cost_scope() as cost:
            sp.input(question)
            res = self._ask(question, session, user)
            res.cost_usd = round(cost.usd, 6)
            res.tokens = {"input": cost.input_tokens, "output": cost.output_tokens, "calls": cost.calls}
            sp.set(**{"tmp.cost_usd": res.cost_usd, "tmp.model_calls": cost.calls,
                      "tmp.tokens.input": cost.input_tokens, "tmp.tokens.output": cost.output_tokens,
                      "tmp.unpriced_calls": cost.unpriced,
                      "langfuse.trace.metadata.cost_usd": f"{res.cost_usd:.6f}",
                      "langfuse.trace.metadata.tokens": f"{cost.input_tokens} in / {cost.output_tokens} out"})
            r = res.reading
            sp.set(**{"tmp.kind": res.kind, "tmp.method": res.method, "tmp.doc_id": res.doc_id,
                      "tmp.tmp_ref": res.tmp_ref, "tmp.resolved_via": res.resolved_via,
                      "tmp.types": r.types if r else [], "tmp.classify": r.method if r else None,
                      "tmp.confidence": r.confidence if r else None,
                      "tmp.context_tokens": res.context.tokens if res.context else 0,
                      "tmp.check_problems": len(res.problems), "tmp.first_check_problems": len(res.first_problems),
                      "tmp.repaired": res.repaired, "tmp.tools": [c["tool"] for c in res.calls],
                      **{f"tmp.latency_ms.{k}": v for k, v in res.latency_ms.items()}})
            sp.set(**{"langfuse.trace.tags": [res.kind, *(r.types if r else [])],
                      "langfuse.trace.metadata.method": res.method, "langfuse.trace.metadata.tmp_ref": res.tmp_ref,
                      "langfuse.trace.metadata.role": role})
            if res.problems:
                sp.set(**{"tmp.check": res.problems[:10], "langfuse.observation.level": "WARNING",
                          "langfuse.observation.status_message": "; ".join(res.problems)[:300]})
            sp.output(res.answer)
            return res

    def _ask(self, question: str, session: Session | None = None, user: User | None = None) -> AnswerResult:
        user = user or User()
        session = session if session is not None else Session()
        lat: dict[str, float] = {}
        t0 = t = time.perf_counter()

        def lap(name: str) -> None:
            nonlocal t
            now = time.perf_counter()
            lat[name] = round((now - t) * 1000, 1)
            t = now

        now = self.now()
        from .details import code_details
        res: Resolution = resolve_tmp(self.store, user, self.cfg, question,
                                      code_details(question, self.cfg.retrieval, now.date()), session)
        lap("resolve")
        if res.status in ("no_access", "no_match", "several"):
            if res.status == "several":
                text = fixed(self.cfg, "ask_back", n=len(res.candidates), options="; ".join(d.label() for d in res.candidates))
                kind = "ask_back"
            else:
                text = fixed(self.cfg, res.status, example=self._example(user))
                kind = res.status
            return self._done(AnswerResult(question, text, kind, "fixed", resolved_via=res.via,
                                           candidates=[d.doc_id for d in res.candidates], latency_ms=lat), t0, session)

        reading = classify(question, session, self.cfg.retrieval, self.model, now)
        lap("classify")
        doc = res.doc
        if doc is None and set(reading.types or ["baseline"]) & (NEEDS_TMP | {"baseline"}):
            text = fixed(self.cfg, "no_tmp", example=self._example(user))
            return self._done(AnswerResult(question, text, "no_tmp", "fixed", reading=reading, latency_ms=lat), t0,
                              session)
        if doc is not None:
            session.use(doc)

        tools = Tools(self.store, self.search, self.cfg, user, doc, load_calendar(self.cfg.retrieval.calendar))
        ev = run_recipes(tools, reading)
        lap("tools")
        ctx = assemble(reading, doc, ev, self.cfg.retrieval.context_budget)
        lap("context")
        out = AnswerResult(question, "", "answer", doc_id=doc.doc_id if doc else None,
                           tmp_ref=doc.tmp_ref if doc else None, resolved_via=res.via, reading=reading,
                           calls=ev.calls, context=ctx, latency_ms=lat)
        if ctx.empty and "out_of_scope" not in reading.types:
            out.answer, out.kind, out.method = fixed(self.cfg, "not_in_tmp", what="anything that answers this question"), \
                "not_in_tmp", "fixed"
            session.remember(question, out.answer, reading.types, reading.details, ev.entities)
            return self._done(out, t0, session)

        answer = write(self.model, reading, ctx, role_style(self.cfg, user.role))
        lap("answer")
        if answer is None:
            out.answer, out.method = clean(safe_answer(reading, ev, ctx, self.cfg, doc)), "safe (no model)"
            out.problems = check_answer(out.answer, ctx)
        else:
            problems = check_answer(answer, ctx)
            out.first_problems = problems
            out.answer, out.method, out.problems = answer, "Q3", problems
            if problems:
                fixed_text = repair(self.model, problems, answer, reading, ctx)
                out.repaired = True
                again = check_answer(fixed_text, ctx) if fixed_text else problems
                if fixed_text and not again:
                    out.answer, out.method, out.problems = fixed_text, "Q4", []
                else:
                    out.answer, out.method = clean(safe_answer(reading, ev, ctx, self.cfg, doc)), "safe (check failed)"
                    out.problems = check_answer(out.answer, ctx)
        lap("check")
        if out.problems:
            lg.log_warning("ask", "answer_check_failed", problems=out.problems, method=out.method)
        session.remember(question, out.answer, reading.types, reading.details, ev.entities)
        return self._done(out, t0, session)

    def _done(self, out: AnswerResult, t0: float, session: Session) -> AnswerResult:
        out.latency_ms["total"] = round((time.perf_counter() - t0) * 1000, 1)
        r = out.reading
        lg.log_event("ask", "answered", question=out.question, standalone=r.standalone if r else None,
                     types=r.types if r else [], classify=r.method if r else None, kind=out.kind, method=out.method,
                     doc_id=out.doc_id, tools=[{"tool": c["tool"], "args": c["args"], "ms": c["ms"]} for c in out.calls],
                     latency_ms=out.latency_ms, context_tokens=out.context.tokens if out.context else 0,
                     check=out.problems, first_check=out.first_problems, repaired=out.repaired)
        return out
