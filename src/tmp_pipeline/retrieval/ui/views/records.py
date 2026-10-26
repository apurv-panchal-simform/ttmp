"""Router decisions per element and the handler records, with each record's evidence cropped from the page."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ....constants import MASTER_DPI
from .. import data, overlay
from . import as_text, current_doc


def render() -> None:
    doc = current_doc()
    st.title("Router & records")
    tab_records, tab_router, tab_logs = st.tabs(["Records", "Router log", "Stage logs"])
    with tab_records:
        _records(doc)
    with tab_router:
        _router(doc)
    with tab_logs:
        _logs(doc)


def _records(doc) -> None:
    recs = data.records_with_ids(doc)
    if not recs:
        st.warning("No records.json in this run folder.")
        return
    df = pd.DataFrame([{"record_id": r["_rid"], "kind": r.get("kind"), "page": r.get("page"),
                        "status": r.get("status"), "entity": r.get("entity"),
                        "handler": (r.get("produced_by") or {}).get("handler"),
                        "model": (r.get("produced_by") or {}).get("model"),
                        "fields": len(r.get("fields") or {}), "evidence": len(r.get("evidence") or []),
                        "flags": as_text(r.get("flags"))} for r in recs])
    c = st.columns(4)
    kinds = c[0].multiselect("Kind", sorted(df["kind"].dropna().unique()))
    statuses = c[1].multiselect("Status", sorted(df["status"].dropna().unique()))
    pages = c[2].multiselect("Page", sorted(df["page"].dropna().unique()))
    text = c[3].text_input("Search (id / entity / flags)")
    view = df
    if kinds:
        view = view[view["kind"].isin(kinds)]
    if statuses:
        view = view[view["status"].isin(statuses)]
    if pages:
        view = view[view["page"].isin(pages)]
    if text:
        t = text.lower()
        view = view[view.apply(lambda r: t in " ".join(map(str, r.values)).lower(), axis=1)]
    s = st.columns(3)
    s[0].bar_chart(view["kind"].value_counts(), horizontal=True, height=220)
    s[1].bar_chart(view["status"].value_counts(), horizontal=True, height=220)
    s[2].bar_chart(view["handler"].value_counts(), horizontal=True, height=220)

    event = st.dataframe(view, hide_index=True, height=360, on_select="rerun", selection_mode="single-row",
                         key="rec_table")
    if not event.selection.rows:
        st.caption("Select a record to see its fields, data and evidence.")
        return
    rid = view.iloc[event.selection.rows[0]]["record_id"]
    rec = next(r for r in recs if r["_rid"] == rid)
    st.subheader(f"{rid} · {rec.get('kind')} · page {rec.get('page')} · {rec.get('status')}")
    left, right = st.columns([3, 2])
    with left:
        if rec.get("fields"):
            st.markdown("**Fields**")
            st.dataframe(pd.DataFrame([{"field": k, **{kk: as_text(vv) for kk, vv in (v or {}).items()}}
                                       for k, v in rec["fields"].items()]), hide_index=True)
        st.markdown("**Data**")
        st.json(rec.get("data") or {}, expanded=1)
        st.markdown("**Produced by**")
        st.json(rec.get("produced_by") or {}, expanded=False)
    with right:
        st.markdown("**Evidence**")
        _evidence(doc, rec)


def _evidence(doc, rec: dict) -> None:
    page = rec.get("page")
    img = data.master(doc, page) if page else None
    info = data.page_info(doc, page) if page else {}
    h = info.get("height_pt")
    dpi = (info.get("master") or {}).get("dpi", MASTER_DPI)
    for ev in rec.get("evidence") or []:
        st.caption(f"`{ev.get('element_id')}`" + (f" — “{ev['quote']}”" if ev.get("quote") else ""))
        b = ev.get("bbox_pt")
        px = overlay.pt_boxes([b], h, dpi)[0] if b and h else None
        if img is not None and px:
            st.image(overlay.crop(img, px), width="stretch")
    if img is not None and h:
        with st.expander("All evidence on the page"):
            boxes = overlay.pt_boxes([ev.get("bbox_pt") for ev in rec.get("evidence") or []], h, dpi)
            st.image(overlay.draw_boxes(img, [(b, "evidence") for b in boxes]), width="stretch")


def _router(doc) -> None:
    rows = data.read_jsonl(doc / "router.log")
    if not rows:
        st.warning("No router.log in this run folder.")
        return
    df = pd.DataFrame(rows).drop(columns=["doc_id", "run_id", "router_version"], errors="ignore")
    c = st.columns(3)
    c[0].bar_chart(df["route"].value_counts(), horizontal=True, height=240)
    c[1].bar_chart(df["outcome"].value_counts(), horizontal=True, height=240)
    c[2].bar_chart(df["rule"].value_counts().head(15), horizontal=True, height=240)
    f = st.columns(3)
    routes = f[0].multiselect("Route", sorted(df["route"].dropna().unique()), key="rt_route")
    outcomes = f[1].multiselect("Outcome", sorted(df["outcome"].dropna().unique()), key="rt_outcome")
    errors_only = f[2].toggle("Errors only", key="rt_err")
    if routes:
        df = df[df["route"].isin(routes)]
    if outcomes:
        df = df[df["outcome"].isin(outcomes)]
    if errors_only:
        df = df[df["error"].notna()]
    st.dataframe(df, hide_index=True, height=520)


def _logs(doc) -> None:
    logs = [n for n in ("run.log", "facts.log", "chunks.log", "di.log") if (doc / n).exists()]
    if not logs:
        st.info("No logs.")
        return
    name = st.radio("Log", logs, horizontal=True)
    rows = data.read_jsonl(doc / name)
    if name == "run.log" and st.toggle("Latest run only", value=True):
        rows = data.last_run(rows)
    df = pd.DataFrame(rows)
    if "level" in df:
        levels = st.multiselect("Level", sorted(df["level"].dropna().unique()))
        if levels:
            df = df[df["level"].isin(levels)]
    if "event" in df:
        events = st.multiselect("Event", sorted(df["event"].dropna().astype(str).unique()))
        if events:
            df = df[df["event"].isin(events)]
    st.dataframe(df.astype(str), hide_index=True, height=520)
