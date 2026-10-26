"""chunks.jsonl: counts per type, a searchable table, and one chunk in full with its lineage."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data
from . import as_text, current_doc


def render() -> None:
    doc = current_doc()
    st.title("Chunks")
    header, rows = data.chunks(doc)
    if not rows:
        st.warning("No chunks.jsonl in this run folder.")
        return
    df = pd.DataFrame([{"chunk_id": c.get("chunk_id"), "type": c.get("chunk_type"), "tokens": c.get("tokens"),
                        "pages": as_text((c.get("metadata") or {}).get("pages")),
                        "header": (c.get("header") or "").replace("\n", " · ")[:160],
                        "has_summary": bool(c.get("summary")), "embedded": bool(c.get("vector_key")),
                        "records": len(c.get("record_ids") or []), "elements": len(c.get("element_ids") or [])}
                       for c in rows])
    c = st.columns(4)
    c[0].metric("Chunks", len(df))
    c[1].metric("Types", df["type"].nunique())
    c[2].metric("Tokens (sum)", int(df["tokens"].fillna(0).sum()))
    c[3].metric("Embedded", int(df["embedded"].sum()))
    with st.expander("Header", expanded=False):
        st.json(header)
    st.bar_chart(df["type"].value_counts(), horizontal=True, height=320)

    a = st.columns([2, 3])
    types = a[0].multiselect("Type", sorted(df["type"].dropna().unique()))
    q = a[1].text_input("Search header / content / summary / keywords")
    view = df
    if types:
        view = view[view["type"].isin(types)]
    if q:
        ql = q.lower()
        hit = {r["chunk_id"] for r in rows if ql in " ".join(
            str(r.get(k) or "") for k in ("header", "content", "summary", "keywords", "chunk_id")).lower()}
        view = view[view["chunk_id"].isin(hit)]
    event = st.dataframe(view, hide_index=True, height=380, on_select="rerun", selection_mode="single-row",
                         key="chunk_table")
    if not event.selection.rows:
        st.caption("Select a chunk to read it in full.")
        return
    cid = view.iloc[event.selection.rows[0]]["chunk_id"]
    ch = next(r for r in rows if r.get("chunk_id") == cid)
    st.subheader(cid)
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Header**")
        st.code(ch.get("header") or "", language=None, wrap_lines=True)
        st.markdown("**Content**")
        st.code(ch.get("content") or "", language=None, wrap_lines=True)
        if ch.get("summary"):
            st.markdown("**Summary**")
            st.write(ch["summary"])
        if ch.get("embedding_text"):
            with st.expander("Embedding text"):
                st.code(ch["embedding_text"], language=None, wrap_lines=True)
    with right:
        st.markdown("**Lineage**")
        st.json({k: ch.get(k) for k in ("parent_id", "siblings", "field_paths", "record_ids", "element_ids")},
                expanded=1)
        st.markdown("**Metadata**")
        st.json(ch.get("metadata") or {}, expanded=1)
        st.markdown(f"**Keywords:** {as_text(ch.get('keywords'))}")
        st.caption(f"tokens {ch.get('tokens')} · hash `{ch.get('content_hash')}` · vector `{ch.get('vector_key')}`")
