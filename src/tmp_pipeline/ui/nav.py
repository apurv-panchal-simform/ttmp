"""The page list, shared by the app and by pages that switch to another page."""
from __future__ import annotations

import streamlit as st

from .views import ask, chunks, diagrams, elements, facts, overview, qa, records, run


def overview_page() -> st.Page:
    return st.Page(overview.render, title="Overview", icon="📋", url_path="overview", default=True)


def pages() -> dict[str, list]:
    return {
        "Document": [
            overview_page(),
            st.Page(elements.render, title="Pages & elements", icon="🗺️", url_path="elements"),
            st.Page(records.render, title="Router & records", icon="🧾", url_path="records"),
            st.Page(diagrams.render, title="Diagrams", icon="📐", url_path="diagrams"),
            st.Page(facts.render, title="Facts & checks", icon="✅", url_path="facts"),
            st.Page(chunks.render, title="Chunks", icon="🧩", url_path="chunks"),
        ],
        "Retrieval": [
            st.Page(ask.render, title="Ask", icon="💬", url_path="ask"),
            st.Page(qa.render, title="QA results", icon="📊", url_path="qa"),
        ],
        "Pipeline": [
            st.Page(run.render, title="Run pipeline", icon="▶️", url_path="run"),
        ],
    }


def open_document(root: str, doc: str) -> None:
    """Select a document and show its overview; the sidebar applies it before its widgets exist."""
    st.session_state["pending_selection"] = (root, doc)
    st.switch_page(overview_page())
