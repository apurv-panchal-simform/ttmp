"""The retrieval question test set (plan 7.2): ask every question, score the answer and the context behind it.

Two kinds of measure. Deterministic ones hold with or without a model and gate offline runs: the rules read the
expected types, the context holds the facts and pages the answer needs, no answer carries a number the context
lacks, and the fixed responses (no match, no TMP, ask-back) fire. Answer measures (answer correct, page cited,
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
from ..model.live import MODES, model_client
from ..retrieval.check import _UNCONFIRMED
from ..retrieval.classify import rule_types
from ..retrieval.hybrid import LocalSearch
from ..retrieval.pipeline import AnswerResult, Retriever
from ..retrieval.resolve import Session
from ..retrieval.store import LocalStore, User

DETERMINISTIC = ("rules_type", "context", "pages_in_context", "no_wrong_number", "fixed_response")
TARGETS = {"no_wrong_number": 1.0, "answer": 0.95, "page_cited": 0.95, "conflict": 1.0, "unconfirmed": 1.0,
           "not_in_tmp": 1.0, "type": 0.95, "rules_type": 1.0, "context": 1.0, "pages_in_context": 1.0,
           "fixed_response": 1.0, "no_invented": 1.0, "json": 0.95}
_NOT_IN = re.compile(r"(?i)doesn't say|does not say|not in the tmp|isn't in|is not in|not covered")
_FIXED = {"no_match": "no_match", "no_tmp": "no_tmp", "ask_back": "ask_back", "not_in_tmp": "not_in_tmp"}


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
    types: list[str]
    row: str = ""                                                  # question group (image set: basic, ocr, spatial ...)
    checks: dict[str, bool] = field(default_factory=dict)       # measure -> passed (absent = not applicable)
    missing: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    repaired: bool = False
    latency_ms: float = 0.0
    answer: str = ""
    cost_usd: float = 0.0


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

    def deterministic_ok(self) -> bool:
        return all((self.rate(m) or 1.0) >= 1.0 for m in DETERMINISTIC)


def score_one(q: dict, res: AnswerResult, rules_types: list[str]) -> QuestionResult:
    want_types = list(q.get("types") or [])
    flags = set(q.get("flags") or [])
    reading = res.reading
    out = QuestionResult(q["id"], q["question"], res.kind, res.method, reading.types if reading else [], q.get("row", ""),
                         problems=res.problems, repaired=res.repaired, latency_ms=res.latency_ms.get("total", 0.0),
                         answer=res.answer, cost_usd=res.cost_usd)
    c = out.checks
    fixed = flags & set(_FIXED)
    if fixed:
        c["fixed_response"] = res.kind in {_FIXED[f] for f in fixed}
    if want_types and reading is not None:
        c["type"] = set(want_types) <= set(reading.types)
    if want_types and q.get("rules"):
        c["rules_type"] = set(want_types) <= set(rules_types)
    ctx = res.context
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
        c["unconfirmed"] = bool(_UNCONFIRMED.search(res.answer))
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
    home = next((d for d in docs if d.tmp_ref == gold.get("document")), None)
    results = []
    for q in gold["questions"]:
        session = Session()
        if q.get("session", gold.get("document")) is not None and home is not None:
            session.use(home)
        for prev in q.get("turns") or []:
            retriever.ask(prev, session, user)
        res = retriever.ask(q["question"], session, user)
        results.append(score_one(q, res, rule_types(q["question"], retriever.cfg.retrieval)))
    return QaScore(results)


def render(s: QaScore) -> str:
    lines = ["Question test set", "", "measure            score    target"]
    for name in ("rules_type", "context", "pages_in_context", "no_wrong_number", "fixed_response", "type", "answer",
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
            lines.append(f"- {r.qid} {r.question!r}: failed {', '.join(failed)}; {r.kind}/{r.method}; types {r.types}"
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
