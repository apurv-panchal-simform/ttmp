"""Ask questions (as ``tmp-pipeline-ask``): pick a TMP from the list you may read, then see the answer, the chunks and
the context behind it, the check and the timing."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from .... import logging_ as lg
from ....config import ConfigError, load_config
from ....errors import PipelineError
from ....llm import model_client
from ...pipeline import AnswerResult, Retriever
from ...search import ChunkSearch, LocalSearch
from ...session import Session, tmp_key
from ...store import FactsStore, LocalStore, User
from .. import data
from . import current_root


def _settings(root: Path) -> dict:
    """No settings on the page: the live model (Azure OpenAI), answers cached under the run folder, chunks from Cosmos
    DB. TMP_UI_MODEL (off | live) and TMP_UI_BACKEND (cosmos | local) change that, for tests and offline use."""
    return {"root": str(root), "mode": os.environ.get("TMP_UI_MODEL", "live"), "fixtures": None,
            "backend": os.environ.get("TMP_UI_BACKEND", "cosmos")}


def _retriever(s: dict) -> Retriever:
    key = (s["root"], s["mode"], s["fixtures"], s["backend"])
    if st.session_state.get("ask_key") != key:
        cfg = load_config()
        local = LocalStore(s["root"])
        store: FactsStore
        search: ChunkSearch
        if s["backend"] == "local":
            store, search = local, LocalSearch(local, cfg)
        else:
            from ....ingestion.embedding.cosmos.cli import backend  # chunks and vectors from Cosmos DB
            store, search = backend(local, cfg)
        cache = Path(s["root"]) / "_model_cache" if s["mode"] == "live" else None
        st.session_state["ask_retriever"] = Retriever(store, search, cfg, model_client(s["mode"], s["fixtures"], cache))
        st.session_state["ask_key"] = key
    return st.session_state["ask_retriever"]


def show_trace(res: AnswerResult) -> None:
    ctx = res.context
    c = st.columns(5)
    c[0].metric("Kind", res.kind)
    c[1].metric("Method", res.method or "–")
    c[2].metric("Total ms", f"{res.latency_ms.get('total', 0):.0f}")
    c[3].metric("Context tokens", ctx.tokens if ctx else 0)
    c[4].metric("Cost $", f"{res.cost_usd:.5f}")
    t_chunks, t_tools, t_ctx, t_check, t_time = st.tabs(["Chunks", "Tools", "Context", "Check", "Timing"])
    with t_chunks:
        if ctx is None or not ctx.hits:
            st.caption("no chunks retrieved")
        else:
            st.dataframe(pd.DataFrame([{"rank": i, "chunk": h.chunk.chunk_id, "type": h.chunk.chunk_type,
                                        "found by": h.via, "score": round(h.score, 4),
                                        "pages": ", ".join(str(p) for p in h.chunk.metadata.get("pages") or [])}
                                       for i, h in enumerate(ctx.hits, 1)]), hide_index=True)
            if ctx.dropped:
                st.caption(f"cut to fit the token budget: {', '.join(ctx.dropped)}")
    with t_tools:
        if not res.tool_calls:
            st.caption("no tools called: the chunks answered the question")
        else:
            st.dataframe(pd.DataFrame([{"tool": c["tool"], "arguments": json.dumps(c["args"], ensure_ascii=False),
                                        "result": json.dumps(c["result"], ensure_ascii=False, default=str)[:300],
                                        "ms": c["ms"]} for c in res.tool_calls]), hide_index=True)
            for c in res.tool_calls:
                with st.expander(f"{c['tool']}  {c['ms']} ms"):
                    st.json({"args": c["args"], "result": c["result"]})
    with t_ctx:
        if ctx is None:
            st.caption("no context assembled")
        else:
            st.caption(f"{ctx.tokens} tokens · pages {sorted(ctx.pages)} · {len(ctx.hits)} chunks"
                       + (" · EMPTY" if ctx.empty else ""))
            st.code(ctx.text, language=None, wrap_lines=True)
    with t_check:
        st.markdown(f"repaired: **{res.repaired}** · cited pages {res.cited_pages}")
        st.markdown("**First answer problems**")
        st.write(res.first_problems or "none")
        st.markdown("**Remaining problems**")
        st.write(res.problems or "none")
    with t_time:
        lat = {k: v for k, v in res.latency_ms.items() if k != "total"}
        if lat:
            st.bar_chart(pd.DataFrame({"step": list(lat), "ms": list(lat.values())}), x="step", y="ms", horizontal=True)
        st.json({"latency_ms": res.latency_ms, "tokens": res.tokens})


def render() -> None:
    root = current_root()
    s = _settings(root)
    st.title("Ask")
    st.session_state.setdefault("ask_history", [])
    st.session_state.setdefault("ask_session", Session())
    user = User()
    try:
        rt = _retriever(s)
        docs = rt.store.docs(user)
    except (PipelineError, ConfigError, ValueError, FileNotFoundError) as e:
        st.error(str(e))
        return
    sess: Session = st.session_state["ask_session"]
    if not docs:
        st.info("No TMP is available to this user in this output folder.")
        return
    names = {f"{tmp_key(d)} · {d.site_address or ', '.join(d.roads[:1]) or d.doc_id[:12]}": d for d in docs}
    sidebar = data.elements(Path(st.session_state["doc"])).get("doc_id") if st.session_state.get("doc") else None
    default = next((i for i, d in enumerate(names.values()) if d.doc_id == sidebar), 0)
    top = st.columns([4, 1])
    chosen = names[top[0].selectbox("TMP to ask about", list(names), index=default, key="ask_tmp")]
    if top[1].button("New session"):
        sess = st.session_state["ask_session"] = Session()
        st.session_state["ask_history"] = []
    if chosen.doc_id != sess.doc_id:                    # a new TMP starts a new conversation
        sess.use(chosen)
        st.session_state["ask_history"] = []
    st.caption(f"{len(st.session_state['ask_history'])} turns")

    for q, res, *rest in st.session_state["ask_history"]:      # turns saved before warnings were kept have none
        warns = rest[0] if rest else []
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            st.write(res.answer)
            _model_warnings(s, res, warns)
            with st.expander("Trace"):
                show_trace(res)

    question = st.chat_input("Ask about this TMP…")
    if not question:
        return
    if s["mode"] == "replay" and not s["fixtures"]:
        st.error("Pick a fixtures folder for replay mode in the sidebar.")
        return
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Answering…"):
                log = Path(s["root"]) / "_ui_ask.log"           # model warnings of this question, shown below it
                run_id = lg.start_run(log)
                try:
                    res = rt.ask(question, sess, user)
                finally:
                    lg.end_run()
                warns = [e for e in data.read_jsonl(log) if e.get("run_id") == run_id
                         and (e.get("level") in ("warning", "error") or e.get("event") == "model_skipped")]
        except (PipelineError, ConfigError, ValueError, FileNotFoundError) as e:
            st.error(str(e))
            return
        st.write(res.answer)
        _model_warnings(s, res, warns)
        with st.expander("Trace", expanded=True):
            show_trace(res)
    st.session_state["ask_history"].append((question, res, warns))


def _model_warnings(s: dict, res: AnswerResult, warns: list[dict]) -> None:
    """Why the model did not answer: model off, or a live call that failed (and its error)."""
    failed = [w for w in warns if w.get("event") in ("live_call_failed", "model_skipped")]
    if s["mode"] == "off" and res.method.startswith("safe"):
        st.info("Model is **off** (sidebar): this is the code-built safe answer. Pick **live** or **replay** for model answers.")
    elif failed:
        with st.expander(f"⚠️ {len(failed)} model call(s) did not answer", expanded=res.method.startswith("safe")):
            for w in failed:
                st.code(f"{w.get('prompt')}: {w.get('error') or w.get('reason')}", language=None, wrap_lines=True)
