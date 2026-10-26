"""Write the answer (Q3), repair it once (Q4) and the fixed replies.

Q3 writes the answer from the context only; Q4 rewrites it once when the check fails. When there is no model, or the
repair still fails, the safe answer is built in code from the same context: the facts and passages with their sources,
so it passes the check by construction.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..config import Config
from ..llm import ModelClient, ask, map_parallel
from . import prompts
from .context import Context
from .search import tokens
from .tools import TOOLS, ToolEnv, entity_catalog, render, run_tool
from .words import rstrip_lines

_CHUNK_CHARS = 420
_MAX_PASSAGES = 4


def role_style(cfg: Config, role: str) -> str:
    styles = cfg.retrieval.role_styles
    return styles.get(role) or styles[cfg.retrieval.default_role]


def fixed(cfg: Config, key: str, **values: object) -> str:
    return cfg.retrieval.responses[key].format(**values)


def _text(out: dict | None) -> str | None:
    return str((out or {}).get("answer") or "").strip() or None


ASK_BACK = "\x00ask_back"          # the model found the question needs a TMP that was not chosen


@dataclass
class ToolCall:
    name: str
    args: dict
    result: dict
    ms: float = 0.0

    def block(self) -> str:
        return render(self.name, self.args, self.result)

    def as_dict(self) -> dict:
        return {"tool": self.name, "args": self.args, "result": self.result, "ms": round(self.ms)}


@dataclass
class Answered:
    answer: str | None                  # None: no model answered
    calls: list[ToolCall] = field(default_factory=list)


def write(model: ModelClient | None, ctx: Context, style: str, now_nz: str, turns: list[str], env: ToolEnv | None = None,
          cfg: Config | None = None, hint: str = "", library: bool = False) -> Answered:
    """Q3, in rounds: the model answers from the context, or asks for tools; the code runs them (calls of one round side
    by side) and the model goes on with the results, until it answers. At most ``max_tool_rounds`` rounds and
    ``max_tool_calls`` calls, then the answer has to be written without tools. With no ``env`` there are no tools."""
    r = cfg.retrieval if cfg else None
    offered = tuple(json.dumps(t) for t in TOOLS) if (env is not None and r and r.tools and r.max_tool_rounds > 0) else ()
    thread: list[str] = []
    calls: list[ToolCall] = []
    entities = entity_catalog(env) if offered and env is not None else ""
    rounds = r.max_tool_rounds if offered and r else 0              # rounds that may call tools; one more writes the answer
    for round_no in range(rounds + 1):
        tools = offered if round_no < rounds and r and len(calls) < r.max_tool_calls else ()
        data = ask(model, prompts.q3_answer(ctx.question, ctx.text, style, now_nz, turns, tools, tuple(thread), library, hint, entities))
        if data is None:
            return Answered(None, calls)
        wanted = data.get("tool_calls") if tools else None
        if not wanted:
            return Answered(ASK_BACK if data.get("ask_back") else _text(data), calls)
        assert env is not None and r is not None
        wanted = wanted[:max(0, r.max_tool_calls - len(calls))]
        thread.append(json.dumps({"role": "assistant", "content": None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}}
            for c in wanted]}))

        def run(c: dict, env: ToolEnv = env) -> ToolCall:
            t0 = time.perf_counter()
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
            return ToolCall(c["name"], args, run_tool(c["name"], args, env), (time.perf_counter() - t0) * 1000)

        done = map_parallel(run, wanted)
        calls += done
        thread += [json.dumps({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(d.result, ensure_ascii=False, default=str)})
                   for c, d in zip(wanted, done, strict=True)]
    return Answered(None, calls)


def repair(model: ModelClient | None, problems: list[str], answer: str, ctx: Context, now_nz: str) -> str | None:
    return _text(ask(model, prompts.q4_repair(problems, answer, ctx.question, ctx.text, now_nz)))


def clean(text: str) -> str:
    return rstrip_lines(text)


# --- the safe answer ---------------------------------------------------------------------------------------------

def _clip(text: str, n: int = _CHUNK_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(",;:") + " …"


def excerpt(content: str, question: str, n: int = _CHUNK_CHARS) -> str:
    """The part of a passage that answers the question: from the line sharing most words with it, onwards."""
    lines = [ln for ln in content.split("\n") if ln.strip()]
    if len(" ".join(content.split())) <= n or len(lines) < 2:
        return _clip(content, n)
    want = set(tokens(question))
    best = max(range(len(lines)), key=lambda i: (len(want & set(tokens(lines[i]))), -i))
    start = best if len(want & set(tokens(lines[best]))) else 0
    return ("… " if start else "") + _clip("\n".join(lines[start:]), n)


def _crumb(header: str) -> str:
    parts = [p.strip() for p in header.split("\n", 1)[0].split("›")][1:]
    return ", ".join(parts).replace(" — page ", ", p.").replace(" — pages ", ", pp.")


def safe_answer(ctx: Context, cfg: Config) -> str:
    """The code-built answer: the best passages with their sources (so it passes the check by construction)."""
    out = [f"Tool result: {b}" for b in ctx.tools[:3]]
    for h in ctx.hits[:_MAX_PASSAGES]:
        md = h.chunk.metadata
        label = ("General standard (the contractor's, not specific to this site): " if md.get("scope") == "generic"
                 else "Struck through (does not apply): " if md.get("applicability") == "not_applicable" else "")
        out.append(f"{label}{excerpt(h.chunk.content, ctx.question)} ({_crumb(h.chunk.header)})")
    return "\n".join(out) if out else fixed(cfg, "not_in_tmp", what="anything that answers this question")
