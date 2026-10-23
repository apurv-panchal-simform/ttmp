"""Ask questions (as ``tmp-pipeline-ask``) and see the whole AnswerResult: reading, tools, context, check, timing."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from ... import logging_ as lg
from ...config import ConfigError, load_config
from ...errors import PipelineError
from ...model.live import MODES, model_client
from ...retrieval.hybrid import ChunkSearch, LocalSearch
from ...retrieval.pipeline import AnswerResult, Retriever
from ...retrieval.resolve import Session, resolve_tmp
from ...retrieval.store import FactsStore, LocalStore, User
from .. import data
from . import current_root

ENV_PREFIXES = ("AZURE_OPENAI_", "AZURE_SEARCH_", "AZURE_COSMOS_", "LANGFUSE_")


def _fixture_dirs(root: Path) -> list[str]:
    return [str(p) for p in sorted(root.glob("_fixtures*")) if p.is_dir()] + \
        [str(p) for p in sorted(root.glob("_fixtures*/*")) if p.is_dir()]


def _settings(root: Path) -> dict:
    sb = st.sidebar
    sb.divider()
    sb.subheader("Ask settings")
    mode = sb.selectbox("Model", MODES, key="ask_mode",
                        help="off: code-built answers · replay: recorded answers · live: Azure OpenAI")
    fixtures = None
    if mode != "off":
        options = _fixture_dirs(root)
        fixtures = sb.selectbox("Fixtures", ["", *options], key="ask_fixtures",
                                help="replay: where answers were recorded · live: where to record (optional)") or None
        if mode == "replay" and not fixtures:
            sb.warning("replay needs a fixtures folder")
    backend = sb.radio("Backend", ["local", "cosmos"], horizontal=True, key="ask_backend")
    role = sb.selectbox("Role", ["", "site", "designer", "office"], key="ask_role",
                        format_func=lambda r: r or "(default)")
    groups = sb.text_input("Access groups", key="ask_groups", help="comma-separated")
    template = sb.text_input("Template TOML (optional)", key="ask_template") or None
    with sb.expander("Environment"):
        names = sorted(k for k in os.environ if k.startswith(ENV_PREFIXES))
        st.caption("Set (values hidden): " + (", ".join(names) or "none"))
    use_sidebar = sb.toggle("Start on the sidebar document", value=True, key="ask_use_sidebar",
                            help="off: the first question must name the TMP, as in tmp-pipeline-ask")
    return {"root": str(root), "mode": mode, "fixtures": fixtures, "backend": backend, "template": template,
            "role": role, "groups": tuple(g.strip() for g in groups.split(",") if g.strip()),
            "use_sidebar": use_sidebar}


def _use_sidebar_doc(rt: Retriever, user: User, sess: Session) -> None:
    """A session with no TMP yet starts on the document picked in the sidebar (matched by its doc id)."""
    doc = st.session_state.get("doc")
    doc_id = (data.elements(Path(doc)).get("doc_id") if doc else None)
    match = next((d for d in rt.store.docs(user) if d.doc_id == doc_id), None)
    if match is not None:
        sess.use(match)


def _retriever(s: dict) -> Retriever:
    key = (s["root"], s["mode"], s["fixtures"], s["backend"], s["template"])
    if st.session_state.get("ask_key") != key:
        cfg = load_config(s["template"])
        local = LocalStore(s["root"])
        store: FactsStore
        search: ChunkSearch
        if s["backend"] == "cosmos":
            from ...cosmos.cli import backend
            store, search = backend(local, cfg)
        else:
            store, search = local, LocalSearch(local, cfg)
        cache = Path(s["root"]) / "_model_cache" if s["mode"] == "live" else None
        st.session_state["ask_retriever"] = Retriever(store, search, cfg, model_client(s["mode"], s["fixtures"], cache))
        st.session_state["ask_key"] = key
    return st.session_state["ask_retriever"]


def show_trace(res: AnswerResult) -> None:
    r = res.reading
    c = st.columns(6)
    c[0].metric("Kind", res.kind)
    c[1].metric("Method", res.method or "–")
    c[2].metric("TMP", res.tmp_ref or "–")
    c[3].metric("Total ms", f"{res.latency_ms.get('total', 0):.0f}")
    c[4].metric("Context tokens", res.context.tokens if res.context else 0)
    c[5].metric("Cost $", f"{res.cost_usd:.5f}")
    t_read, t_tools, t_ctx, t_check, t_time = st.tabs(["Reading", "Tools", "Context", "Check", "Timing"])
    with t_read:
        st.markdown(f"resolved via **{res.resolved_via or '–'}** · doc `{res.doc_id or '–'}`")
        if r:
            st.json({"standalone": r.standalone, "types": r.types, "rule_types": r.rule_types, "method": r.method,
                     "confidence": r.confidence, "follow_up": r.follow_up, "details": r.details,
                     "problems": r.problems})
        if res.candidates:
            st.markdown("**Ask-back candidates:** " + ", ".join(res.candidates))
    with t_tools:
        if res.calls:
            st.dataframe(pd.DataFrame([{"tool": x.get("tool"), "args": str(x.get("args")), "ms": x.get("ms")}
                                       for x in res.calls]), hide_index=True)
        else:
            st.caption("no tool calls")
    with t_ctx:
        ctx = res.context
        if ctx is None:
            st.caption("no context assembled")
        else:
            st.caption(f"{ctx.tokens} tokens · pages {sorted(ctx.pages)} · {len(ctx.chunk_ids)} chunks · "
                       f"{len(ctx.dropped)} dropped · {len(ctx.conflicts)} conflicts" + (" · EMPTY" if ctx.empty else ""))
            st.code(ctx.text, language=None, wrap_lines=True)
            with st.expander("Chunk ids"):
                st.json({"used": ctx.chunk_ids, "dropped": ctx.dropped})
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
    top = st.columns([3, 1, 1])
    start_tmp = top[0].text_input("Start on TMP (reference or address, optional)", key="ask_tmp")
    if top[1].button("Use TMP", disabled=not start_tmp):
        try:
            rt = _retriever(s)
            found = resolve_tmp(rt.store, User(s["groups"], s["role"]), rt.cfg, start_tmp, {}, None)
            if found.doc is None:
                st.error(f"No single TMP matches {start_tmp!r} ({found.status}).")
            else:
                st.session_state["ask_session"].use(found.doc)
        except (PipelineError, ConfigError, ValueError, FileNotFoundError) as e:
            st.error(str(e))
    if top[2].button("New session"):
        st.session_state["ask_session"] = Session()
        st.session_state["ask_history"] = []
    sess: Session = st.session_state["ask_session"]
    st.caption(f"session TMP: **{sess.tmp_ref or 'none'}** · {len(st.session_state['ask_history'])} turns")

    for q, res, *rest in st.session_state["ask_history"]:      # turns saved before warnings were kept have none
        warns = rest[0] if rest else []
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            st.write(res.answer)
            _model_warnings(s, res, warns)
            with st.expander("Trace"):
                show_trace(res)

    question = st.chat_input("Ask about the processed TMPs…")
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
                rt, user = _retriever(s), User(s["groups"], s["role"])
                if sess.doc_id is None and s["use_sidebar"]:
                    _use_sidebar_doc(rt, user, sess)
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
