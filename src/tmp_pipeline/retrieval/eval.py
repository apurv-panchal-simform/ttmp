"""The retrieval question test set (plan 7.2): ask every question, score the answer and the context behind it.

Two kinds of measure. Deterministic ones hold with or without a model and gate offline runs: the context holds the
facts and pages the answer needs, no answer carries a number the context lacks, and the fixed responses (no match,
ask-back) fire. Answer measures (answer correct, page cited,
conflicts and unconfirmed values mentioned, first words p95) are meaningful with a model (``--model live`` or
``replay``); without one they score the code-built safe answer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import tracing
from ..config import Config, ConfigError, load_config
from ..errors import PipelineError
from ..llm import MODES, model_client
from .check import says_unconfirmed
from .pipeline import AnswerResult, Retriever
from .search import LocalSearch
from .session import Session
from .store import LocalStore, User

DETERMINISTIC = ("context", "pages_in_context", "no_wrong_number", "fixed_response")
TARGETS = {"no_wrong_number": 1.0, "answer": 0.95, "page_cited": 0.95, "conflict": 1.0, "unconfirmed": 1.0,
           "not_in_tmp": 1.0, "context": 1.0, "pages_in_context": 1.0,
           "fixed_response": 1.0, "no_invented": 1.0, "json": 0.95}
_NOT_IN = re.compile(r"(?i)doesn't say|does not say|not in the tmp|isn't in|is not in|not covered")
_FIXED = {"no_match": "no_match", "no_tmp": "ask_back", "ask_back": "ask_back", "not_in_tmp": "not_in_tmp"}


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def _spellings(want: str) -> list[str]:
    """The ways an answer may write the same value: 2026-09-28 / 28 September 2026; 15:00 / 1500 / 3 pm."""
    out = [want]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", want)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out += [f"{d} {_MONTHS[mo - 1]} {y}", f"{d} {_MONTHS[mo - 1][:3]} {y}", f"{_MONTHS[mo - 1]} {d}, {y}",
                f"{d}/{mo}/{y}", f"{d:02d}/{mo:02d}/{y}"]
    m = re.fullmatch(r"(\d{2}):(\d{2})", want)
    if m:
        h, mi = int(m.group(1)), m.group(2)
        twelve = f"{h % 12 or 12}" + (f":{mi}" if mi != "00" else "")
        out += [f"{h:02d}{mi}", f"{h}:{mi}", f"{twelve} {'pm' if h >= 12 else 'am'}", f"{twelve}{'pm' if h >= 12 else 'am'}"]
    return out


def _has(text: str, want: str) -> bool:
    hay = " ".join(text.lower().split())
    return any(" ".join(w.lower().split()) in hay for w in _spellings(want))


@dataclass
class QuestionResult:
    qid: str
    question: str
    kind: str
    method: str
    row: str = ""                                                  # question group (image set: basic, ocr, spatial ...)
    checks: dict[str, bool] = field(default_factory=dict)       # measure -> passed (absent = not applicable)
    missing: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    repaired: bool = False
    latency_ms: float = 0.0
    answer: str = ""
    cost_usd: float = 0.0
    context_tokens: int = 0                                        # retrieval metrics (informational, never gate a run)
    chunks_in_context: int = 0
    chunk_ranks: dict[str, int | None] = field(default_factory=dict)   # expected context string -> rank of first chunk holding it


@dataclass
class QaScore:
    results: list[QuestionResult]

    def measure(self, name: str) -> tuple[int, int]:
        vals = [r.checks[name] for r in self.results if name in r.checks]
        return sum(vals), len(vals)

    def rate(self, name: str) -> float | None:
        ok, n = self.measure(name)
        return ok / n if n else None

    def p95_ms(self) -> float:
        lat = sorted(r.latency_ms for r in self.results)
        return lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else 0.0

    def retrieval(self) -> dict[str, float]:
        """Retrieval quality of the chunks alone. ``chunk_hit`` is the share of expected context strings found in some
        retrieved chunk (the rest come from facts or tools); ``mrr`` and ``hit@1/3`` are over those found in a chunk."""
        ranks = [rk for r in self.results for rk in r.chunk_ranks.values()]
        found = [rk for rk in ranks if rk]
        n = len(self.results) or 1
        return {"strings": len(ranks), "chunk_hit": len(found) / len(ranks) if ranks else 0.0,
                "mrr": sum(1 / rk for rk in found) / len(found) if found else 0.0,
                "hit@1": sum(rk <= 1 for rk in found) / len(found) if found else 0.0,
                "hit@3": sum(rk <= 3 for rk in found) / len(found) if found else 0.0,
                "context_tokens": sum(r.context_tokens for r in self.results) / n,
                "chunks": sum(r.chunks_in_context for r in self.results) / n}

    def deterministic_ok(self) -> bool:
        return all((self.rate(m) or 1.0) >= 1.0 for m in DETERMINISTIC)


_CHUNK_BLOCK = re.compile(r"(?m)^\[(\d+)\] ")


def chunk_ranks(text: str, wanted: list[str]) -> dict[str, int | None]:
    """For each wanted string, the rank (1 = best) of the first retrieved chunk block that holds it, None if no chunk does.

    The assembled context lists its chunks best first as ``[i] breadcrumb [tags]`` blocks after a CHUNKS line."""
    head, sep, tail = text.partition("\nCHUNKS\n")
    blocks: list[str] = []
    if sep:
        tail = re.split(r"\n(?:TOOL RESULTS|CONFLICTS ON VALUES USED|NOT CHECKED)\n", tail)[0]
        starts = [m.start() for m in _CHUNK_BLOCK.finditer(tail)]
        blocks = [tail[a:b] for a, b in zip(starts, starts[1:] + [len(tail)])]
    out: dict[str, int | None] = {}
    for w in wanted:
        out[w] = next((i for i, b in enumerate(blocks, 1) if _has(b, w)), None)
    return out


def score_one(q: dict, res: AnswerResult) -> QuestionResult:
    flags = set(q.get("flags") or [])
    out = QuestionResult(q["id"], q["question"], res.kind, res.method, q.get("row", ""),
                         problems=res.problems, repaired=res.repaired, latency_ms=res.latency_ms.get("total", 0.0),
                         answer=res.answer, cost_usd=res.cost_usd)
    c = out.checks
    fixed = flags & set(_FIXED)
    if fixed:
        c["fixed_response"] = res.kind in {_FIXED[f] for f in fixed}
    ctx = res.context
    if ctx is not None:
        out.context_tokens, out.chunks_in_context = ctx.tokens, len(ctx.chunk_ids)
        if q.get("context"):
            out.chunk_ranks = chunk_ranks(ctx.text, list(q["context"]))
    if q.get("context"):
        miss = [s for s in q["context"] if not (ctx and _has(ctx.text, s))]
        c["context"] = not miss
        if miss:
            out.missing["context"] = miss
    if q.get("pages") and ctx is not None and not ctx.empty:
        miss_p = [p for p in q["pages"] if p not in ctx.pages]
        c["pages_in_context"] = not miss_p
        if miss_p:
            out.missing["pages_in_context"] = [str(p) for p in miss_p]
    if res.kind == "answer":
        c["no_wrong_number"] = not any(p.startswith("number ") for p in res.problems)
    miss_a = [s for s in q.get("answer") or [] if not _has(res.answer, s)]
    c["answer"] = not miss_a and (not fixed or c.get("fixed_response", True))
    if miss_a:
        out.missing["answer"] = miss_a
    if q.get("pages") and res.kind == "answer":
        c["page_cited"] = bool(set(q["pages"]) & set(res.cited_pages))
    if "conflict" in flags:
        miss_c = [s for s in q.get("conflict_values") or [] if not _has(res.answer, s)]
        c["conflict"] = not miss_c
        if miss_c:
            out.missing["conflict"] = miss_c
    if "unconfirmed" in flags:
        c["unconfirmed"] = says_unconfirmed(res.answer)
    if "not_in_tmp" in flags:
        c["not_in_tmp"] = bool(_NOT_IN.search(res.answer)) or res.kind == "not_in_tmp"
    if q.get("must_not") and res.kind == "answer":
        bad = [x for x in q["must_not"] if _has(res.answer, x)]
        c["no_invented"] = not bad
        if bad:
            out.missing["no_invented"] = bad
    if "json" in flags and res.kind == "answer":
        obj = _json_in(res.answer)
        miss_k = [k for k in q.get("json_keys") or [] if obj is None or k not in obj]
        c["json"] = obj is not None and not miss_k
        if miss_k:
            out.missing["json"] = miss_k
    return out


def _json_in(text: str) -> dict | None:
    """The first JSON object written anywhere in an answer."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    break
        start = text.find("{", start + 1)
    return None


def run_questions(retriever: Retriever, gold: dict, user: User | None = None) -> QaScore:
    user = user or User()
    docs = retriever.store.docs(user)
    home = max((d for d in docs if d.tmp_ref == gold.get("document")), key=lambda d: (d.version, d.written),
               default=None)                              # the latest version when several are stored
    results = []
    for q in gold["questions"]:
        session = Session()
        if q.get("session", gold.get("document")) is not None and home is not None:
            session.use(home)
        for prev in q.get("turns") or []:
            retriever.ask(prev, session, user)
        res = retriever.ask(q["question"], session, user)
        results.append(score_one(q, res))
    return QaScore(results)


def render(s: QaScore) -> str:
    lines = ["Question test set", "", "measure            score    target"]
    for name in ("context", "pages_in_context", "no_wrong_number", "fixed_response", "answer",
                 "page_cited", "conflict", "unconfirmed", "not_in_tmp", "no_invented", "json"):
        ok, n = s.measure(name)
        if n:
            tag = " (offline gate)" if name in DETERMINISTIC else ""
            lines.append(f"{name:18s} {ok:>3}/{n:<3}  {TARGETS.get(name, 0) * 100:.0f}%{tag}")
    lines.append(f"{'repairs':18s} {sum(r.repaired for r in s.results):>3}/{len(s.results):<3}  tracked")
    lines.append(f"{'latency p95':18s} {s.p95_ms():.0f} ms (target: first words < 3000 ms)")
    spent = sum(r.cost_usd for r in s.results)
    lines.append(f"{'model cost':18s} ${spent:.4f} total, ${spent / max(1, len(s.results)):.4f} per question "
                 "(live calls only; cached and replayed answers are free)")
    m = s.retrieval()
    lines.append(f"{'retrieval':18s} chunk_hit {m['chunk_hit'] * 100:.0f}% of {m['strings']:.0f} strings; MRR {m['mrr']:.3f}; "
                 f"hit@1 {m['hit@1'] * 100:.0f}%; hit@3 {m['hit@3'] * 100:.0f}%; "
                 f"{m['context_tokens']:.0f} context tokens, {m['chunks']:.1f} chunks per question (informational)")
    rows = list(dict.fromkeys(r.row for r in s.results if r.row))
    if len(rows) > 1:
        lines += ["", "by group           answer   context"]
        for row in rows:
            rs = [r for r in s.results if r.row == row]
            a = [r.checks["answer"] for r in rs if "answer" in r.checks]
            c = [r.checks["context"] for r in rs if "context" in r.checks]
            lines.append(f"{row:18s} {sum(a):>3}/{len(a):<3}  {sum(c):>3}/{len(c):<3}")
    lines.append("")
    for r in s.results:
        failed = [k for k, v in r.checks.items() if not v]
        if failed:
            lines.append(f"- {r.qid} {r.question!r}: failed {', '.join(failed)}; {r.kind}/{r.method}"
                         + "".join(f"; missing {k}: {v}" for k, v in r.missing.items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None, backend: Callable[[LocalStore, Config], tuple[Any, Any]] | None = None) -> int:
    """``backend`` swaps the store and search (the optional ``cosmos`` package passes its own); default local."""
    ap = argparse.ArgumentParser(prog="tmp-pipeline-qa", description="Run the retrieval question test set (plan 7.2)")
    ap.add_argument("questions", help="question file, for example tests/data/gold/sample.questions.json")
    ap.add_argument("--out", default="out", help="output root holding the processed TMPs (default out/)")
    ap.add_argument("--template", default=None)
    ap.add_argument("--model", choices=MODES, default="off")
    ap.add_argument("--model-fixtures", default=None, help="replay: recorded answers; live: where to record them")
    ap.add_argument("--json", default=None, help="write per-question results to this file")
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    try:
        cfg = load_config(a.template)
        gold = json.loads(Path(a.questions).read_text(encoding="utf-8"))
        model = model_client(a.model, a.model_fixtures)
        local = LocalStore(a.out)
        store, search = backend(local, cfg) if backend else (local, LocalSearch(local, cfg))
        today = dt.datetime.fromisoformat(gold["today"]) if gold.get("today") else None
        retriever = Retriever(store, search, cfg, model, (lambda: today) if today else None)
        score = run_questions(retriever, gold)
    except (PipelineError, ConfigError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(render(score))
    if a.json:
        Path(a.json).write_text(json.dumps([r.__dict__ for r in score.results], indent=1, ensure_ascii=False),
                                encoding="utf-8")
    return 0 if score.deterministic_ok() else 1


if __name__ == "__main__":
    sys.exit(main())
