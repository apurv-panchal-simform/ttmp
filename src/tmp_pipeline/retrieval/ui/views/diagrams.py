"""Diagram sheets (sheets.json): title block, legend, notes, roads, regions, symbols and images over the render."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ....constants import MASTER_DPI
from .. import data, overlay
from . import as_text, current_doc

LAYERS = {"symbols": "symbols", "regions": "regions", "images": "images", "legend": "legend"}


def render() -> None:
    doc = current_doc()
    st.title("Diagrams")
    sheets = data.sheets(doc)
    if not sheets:
        st.info("No diagram sheets in this document (sheets.json is empty or missing).")
        return
    labels = [f"Sheet {s.get('sheet')} · {s.get('title')} · page {s.get('page')}" for s in sheets]
    i = st.selectbox("Sheet", range(len(sheets)), format_func=lambda n: labels[n]) or 0
    s = sheets[i]

    c = st.columns(5)
    c[0].metric("Page", s.get("page"))
    c[1].metric("Rev", s.get("rev") or "–")
    c[2].metric("Symbols", len(s.get("symbols") or []))
    c[3].metric("Regions", len(s.get("regions") or []))
    c[4].metric("Vector", "yes" if s.get("vector") else "no")

    img_col, side = st.columns([3, 2])
    with side:
        base = st.radio("Image", ["master", "overlay"], horizontal=True)
        layers = st.multiselect("Layers", list(LAYERS), default=["symbols"])
        sym_kinds = sorted({x.get("kind") for x in s.get("symbols") or []} - {None})
        kinds = st.multiselect("Symbol kinds", sym_kinds, default=[k for k in sym_kinds if k != "line"])
        st.markdown("**Title block**")
        st.dataframe(pd.DataFrame([{"field": k, "value": as_text(v)} for k, v in (s.get("title_block") or {}).items()]),
                     hide_index=True)
        st.markdown("**Roads**")
        st.dataframe(pd.DataFrame(s.get("roads") or []), hide_index=True)
        st.markdown("**Legend**")
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items() if k != "bbox"} for x in s.get("legend") or []]),
                     hide_index=True)
    with img_col:
        img = data.overlay(doc, s["page"]) if base == "overlay" else data.master(doc, s["page"])
        if img is None:
            st.info("No render for this page.")
        else:
            info = data.page_info(doc, s["page"])
            h, dpi = info.get("height_pt"), (info.get("master") or {}).get("dpi", MASTER_DPI)
            boxes: list[tuple[list[float] | None, str]] = []
            for layer in layers:
                items = s.get(LAYERS[layer]) or []
                if layer == "symbols":
                    items = [x for x in items if x.get("kind") in kinds]
                cats = [f"{layer}:{x.get('kind') or x.get('role') or x.get('swatch') or ''}" for x in items]
                if h:
                    boxes += list(zip(overlay.pt_boxes([x.get("bbox") for x in items], h, dpi), cats, strict=True))
            st.image(overlay.draw_boxes(img, boxes, width=2), width="stretch")
            legend = sorted({c for _, c in boxes})
            st.markdown(" ".join(f"<span style='color:rgb{overlay.colour(v)}'>■</span> {v}" for v in legend),
                        unsafe_allow_html=True)

    t_sym, t_reg, t_img, t_notes, t_raw = st.tabs(["Symbols", "Regions", "Images", "Notes & callouts", "Raw JSON"])
    with t_sym:
        rows = [{k: as_text(v) for k, v in x.items() if k not in ("objects", "extra")} for x in s.get("symbols") or []]
        st.dataframe(pd.DataFrame(rows), hide_index=True, height=420)
    with t_reg:
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items()} for x in s.get("regions") or []]),
                     hide_index=True)
    with t_img:
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items()} for x in s.get("images") or []]),
                     hide_index=True)
    with t_notes:
        for n in s.get("notes") or []:
            st.markdown(f"- {n}")
        if s.get("callouts"):
            st.markdown("**Callouts**")
            for n in s["callouts"]:
                st.markdown(f"- {n}")
        if s.get("low_quality_lines"):
            st.markdown("**Low-quality text lines**")
            for n in s["low_quality_lines"]:
                st.code(n)
    with t_raw:
        st.json(s, expanded=1)
