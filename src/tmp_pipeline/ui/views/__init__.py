"""One module per UI page; each exposes ``render()``."""
from __future__ import annotations

from pathlib import Path

import streamlit as st


def current_doc() -> Path:
    """The document picked in the sidebar; stops the page when there is none."""
    doc = st.session_state.get("doc")
    if not doc:
        st.info("Pick an output root with processed documents in the sidebar, or run the pipeline first.")
        st.stop()
    return Path(doc)


def current_root() -> Path:
    return Path(st.session_state.get("root") or "out")


def as_text(v) -> str:
    """Lists and dicts flattened for a dataframe cell."""
    if v is None:
        return ""
    if isinstance(v, list | tuple):
        return ", ".join(as_text(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}={as_text(x)}" for k, x in v.items())
    return str(v)
