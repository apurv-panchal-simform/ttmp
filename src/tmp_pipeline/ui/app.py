"""TMP pipeline UI: pick an output root and a document in the sidebar, then browse, ask or run.

    streamlit run src/tmp_pipeline/ui/app.py -- --out out_live      (or: tmp-pipeline-ui --out out_live)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import streamlit as st

from tmp_pipeline import tracing
from tmp_pipeline.ui import data, nav

st.set_page_config(page_title="TMP pipeline", page_icon="🚧", layout="wide")


@st.cache_resource
def _tracing() -> bool:
    tracing.configure()            # exports spans to Langfuse only when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
    return True


def _default_root() -> str:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--out", default=None)
    a, _ = ap.parse_known_args()
    if a.out:
        return a.out
    return next((r for r in ("out_live", "out") if Path(r).is_dir()), "out")


def sidebar() -> None:
    st.session_state.setdefault("root", _default_root())
    pending = st.session_state.pop("pending_selection", None)
    if pending:
        st.session_state["root"], st.session_state["doc"] = pending
    st.sidebar.title("🚧 TMP pipeline")
    root = st.sidebar.text_input("Output root", key="root", help="folder holding sha256-* run folders")
    docs = data.list_docs(Path(root))
    if not docs:
        st.sidebar.info("No processed documents here yet. Use **Run pipeline**.")
        st.session_state["doc"] = None
        return
    paths = [str(d.path) for d in docs]
    if st.session_state.get("doc") not in paths:
        st.session_state["doc"] = paths[0]
    labels = {str(d.path): d.label for d in docs}
    st.sidebar.selectbox("Document", paths, key="doc", format_func=lambda p: labels[p])


_tracing()
sidebar()
st.navigation(nav.pages()).run()
