"""``tmp-pipeline-ask``: ask questions about processed TMPs from the terminal (one question, or a session)."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from .. import tracing
from ..config import Config, ConfigError, load_config
from ..errors import PipelineError
from ..llm import MODES, model_client
from .pipeline import AnswerResult, Retriever
from .search import ChunkSearch, LocalSearch
from .session import Session, find_tmp
from .store import FactsStore, LocalStore, User


def _show(res: AnswerResult, verbose: bool) -> None:
    print(res.answer)
    if verbose:
        ctx = res.context
        print(f"\n[{res.kind} · {res.method} · {res.tmp_ref or 'no TMP'} · {res.latency_ms.get('total', 0):.0f} ms · "
              f"context {ctx.tokens if ctx else 0} tokens · {len(ctx.hits) if ctx else 0} chunks · "
              f"{res.tokens.get('calls', 0)} model calls]")
        for h in ctx.hits if ctx else []:
            print(f"  chunk {h.chunk.chunk_id} ({h.via}, {h.score:.3f})")
        if res.problems:
            print(f"  check: {'; '.join(res.problems)}")


Backend = Callable[[LocalStore, Config], tuple[FactsStore, ChunkSearch]]


def main(argv: list[str] | None = None, backend: Backend | None = None) -> int:
    """``backend`` swaps the store and search (the optional ``cosmos`` package passes its own); default local."""
    ap = argparse.ArgumentParser(prog="tmp-pipeline-ask", description="Ask a question about the processed TMPs")
    ap.add_argument("question", nargs="*", help="the question; leave out for an interactive session")
    ap.add_argument("--out", default="out", help="output root holding the processed TMPs (default out/)")
    ap.add_argument("--tmp", default=None, help="answer from this TMP (key, reference or address); needed when several are readable")
    ap.add_argument("--template", default=None)
    ap.add_argument("--model", choices=MODES, default="off",
                    help="off: code-built answers (default); replay: recorded answers; live: Azure OpenAI")
    ap.add_argument("--model-fixtures", default=None, help="replay: recorded answers; live: where to record them")
    ap.add_argument("--role", default="", help="answer style: site, designer or office")
    ap.add_argument("--groups", default="", help="comma-separated access groups of the user")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the chunks used, timing and the check")
    a = ap.parse_args(argv)
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    try:
        cfg = load_config(a.template)
        local = LocalStore(a.out)
        store, search = backend(local, cfg) if backend else (local, LocalSearch(local, cfg))
        retriever = Retriever(store, search, cfg, model_client(a.model, a.model_fixtures))
        user = User(tuple(g for g in a.groups.split(",") if g), a.role)
        session = Session()
        if a.tmp:
            res = find_tmp(store, user, a.tmp)
            if res.doc is None:
                print(f"error: no single TMP matches {a.tmp!r}", file=sys.stderr)
                return 2
            session.use(res.doc)
        if a.question:
            _show(retriever.ask(" ".join(a.question), session, user), a.verbose)
            return 0
        while True:
            try:
                q = input("> ").strip()
            except EOFError:
                return 0
            if q in ("", "exit", "quit"):
                return 0
            _show(retriever.ask(q, session, user), a.verbose)
            print()
    except (PipelineError, ConfigError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
