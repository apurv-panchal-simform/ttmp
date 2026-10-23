"""A page render with every element boxed (coloured by type, zone, route or outcome), or with the chunks drawn over
the elements they were built from, and the element table."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data, overlay
from . import current_doc

NOT_IN_CHUNK = "! not in any chunk"


def _union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def render() -> None:
    doc = current_doc()
    st.title("Pages & elements")
    df = data.elements_df(doc)
    if df.empty:
        st.warning("No elements.json in this run folder.")
        return
    by_element, chunk_rows = data.chunk_index(doc)
    df["chunks"] = df["element_id"].map(lambda e: len(by_element.get(e, [])))
    pages = sorted(df["page"].unique().tolist())
    c = st.columns([3, 2, 2, 2])
    page = c[0].select_slider("Page", options=pages, key="el_page")
    colour_by = c[1].selectbox("Colour by", ["type", "route", "outcome", "zone", "chunk"], key="el_colour",
                               help="chunk: one box per chunk, around the elements it was built from")
    image_kind = c[2].radio("Image", ["master", "diagram overlay"], horizontal=True, key="el_image")
    show_boxes = c[3].toggle("Boxes", value=True, key="el_boxes")

    rows = df[df["page"] == page]
    f = st.columns(3)
    types = f[0].multiselect("Type", sorted(rows["type"].dropna().unique()), key="el_types")
    routes = f[1].multiselect("Route", sorted(rows["route"].dropna().unique()), key="el_routes")
    outcomes = f[2].multiselect("Outcome", sorted(rows["outcome"].dropna().unique()), key="el_outcomes")
    if types:
        rows = rows[rows["type"].isin(types)]
    if routes:
        rows = rows[rows["route"].isin(routes)]
    if outcomes:
        rows = rows[rows["outcome"].isin(outcomes)]
    rows = rows.reset_index(drop=True)

    info = data.page_info(doc, page)
    st.caption(f"zone **{info.get('zone')}** ({info.get('zone_reason')}) · form {info.get('form_name')} · "
               f"{len(rows)} elements shown · boxes are elements (one per text block, cell, symbol …); "
               f"pick **Colour by: chunk** to see the chunks")

    chunk_boxes: list[tuple[str, list[float]]] = []
    picked_chunk = None
    if colour_by == "chunk":
        on_page: dict[str, list[list[float]]] = {}
        for eid, b in zip(rows["element_id"], rows["bbox_px"], strict=True):
            for cid in by_element.get(eid, []):
                if b:
                    on_page.setdefault(cid, []).append(b)
        types_here = sorted({chunk_rows[c]["chunk_type"] for c in on_page})
        g = st.columns([2, 3, 1])
        keep = g[0].multiselect("Chunk types", types_here, default=[t for t in types_here if t != "doc_card"],
                                key="el_chunk_types", help="the document card summarises the whole TMP, so it is hidden by default")
        chunk_boxes = [(cid, _union(bs)) for cid, bs in on_page.items() if chunk_rows[cid]["chunk_type"] in keep]
        picked_chunk = g[1].selectbox("Chunk", ["", *[cid for cid, _ in chunk_boxes]], key="el_chunk",
                                      format_func=lambda x: x.split(":", 1)[-1] if x else "(pick one to read it)")
        orphans = g[2].toggle("Not in any chunk", value=True, key="el_orphans",
                              help="elements with text that no chunk was built from, in red")

    img_col, table_col = st.columns([3, 2])
    with table_col:
        event = st.dataframe(rows.drop(columns=["bbox_px", "bbox_pt", "page"]), hide_index=True, height=640,
                             on_select="rerun", selection_mode="multi-row", key=f"el_table_{page}")
        picked = set(event.selection.rows) if event and event.selection else set()
    with img_col:
        img = data.overlay(doc, page) if image_kind == "diagram overlay" else data.master(doc, page)
        if img is None:
            st.info("No diagram overlay for this page." if image_kind == "diagram overlay"
                    else "No page render (run without --skip-render).")
        else:
            if show_boxes and colour_by == "chunk":
                boxes: list[tuple[list[float] | None, str]] = [(b, cid) for cid, b in chunk_boxes]
                hi = {i for i, (cid, _) in enumerate(chunk_boxes) if cid == picked_chunk}
                if orphans:
                    lonely = rows[(rows["chunks"] == 0) & (rows["text"].str.strip() != "")
                                  & (rows["type"] != "page_furniture")]
                    boxes += [(b, NOT_IN_CHUNK) for b in lonely["bbox_px"]]
                img = overlay.draw_boxes(img, boxes, hi, width=4, fill_alpha=25)
                legend = [NOT_IN_CHUNK] if orphans else []
            elif show_boxes:
                boxes = [(b, str(v)) for b, v in zip(rows["bbox_px"], rows[colour_by], strict=True)]
                img = overlay.draw_boxes(img, boxes, picked)
                legend = sorted(rows[colour_by].dropna().astype(str).unique())
            else:
                legend = []
            st.image(img, width="stretch")
            if legend:
                st.markdown(" ".join(f"<span style='color:rgb{overlay.colour(v)}'>■</span> {v}" for v in legend),
                            unsafe_allow_html=True)

    if colour_by == "chunk":
        if picked_chunk:
            ch = chunk_rows[picked_chunk]
            st.subheader(picked_chunk)
            st.caption(f"{ch.get('chunk_type')} · {ch.get('tokens')} tokens")
            st.code(ch.get("header") or "", language=None, wrap_lines=True)
            st.code(ch.get("content") or "", language=None, wrap_lines=True)
        st.dataframe(pd.DataFrame([{"chunk_id": cid, "type": chunk_rows[cid]["chunk_type"],
                                    "tokens": chunk_rows[cid].get("tokens"),
                                    "first line": (chunk_rows[cid].get("content") or "").split("\n", 1)[0][:100]}
                                   for cid, _ in chunk_boxes]), hide_index=True)

    if picked:
        full = data.elements(doc).get("elements", [])
        by_id = {e["element_id"]: e for e in full}
        for i in sorted(picked):
            eid = rows.loc[i, "element_id"]
            with st.expander(f"Element {eid} · in chunks: {', '.join(by_element.get(eid, [])) or 'none'}",
                             expanded=len(picked) == 1):
                st.json(by_id.get(eid), expanded=2)
