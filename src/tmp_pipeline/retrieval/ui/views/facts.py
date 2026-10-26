"""facts.json: fields with sources, TMD sheets, mentions, sign-offs, conflicts, the 10 checks and the review queue."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data
from . import as_text, current_doc

OUTCOME_ICON = {"pass": "✅", "conflict": "⚠️", "flagged": "🚩", "fail": "❌", "not_checked": "➖"}


def _fields(facts: list[dict]) -> pd.DataFrame:
    rows = []
    for f in facts:
        for name, v in (f.get("fields") or {}).items():
            v = v or {}
            rows.append({"fact_id": f.get("fact_id"), "entity": f.get("entity"), "field": name,
                         "value": as_text(v.get("value")), "unit": v.get("unit"), "status": v.get("status"),
                         "method": v.get("method"),
                         "pages": as_text(sorted({s.get("page") for s in v.get("sources") or [] if s.get("page")})),
                         "records": as_text([s.get("record_id") for s in v.get("sources") or []]),
                         "raw": as_text(v.get("raw")), "notes": as_text(v.get("notes"))})
    return pd.DataFrame(rows)


def render() -> None:
    doc = current_doc()
    st.title("Facts & checks")
    f = data.facts(doc)
    if not f:
        st.warning("No facts.json in this run folder.")
        return
    checks = f.get("checks") or []
    c = st.columns(5)
    c[0].metric("Facts", len(f.get("facts") or []))
    c[1].metric("Conflicts", len(f.get("conflicts") or []))
    c[2].metric("Checks not passing", sum(1 for x in checks if x.get("outcome") != "pass"))
    c[3].metric("Review items", len(f.get("review") or []))
    c[4].metric("Revision", f.get("revision", "–"))

    tabs = st.tabs(["Fields", "Checks", "Conflicts", "TMD sheets", "Mentions", "Sign-offs", "Review queue",
                    "Other records", "Raw JSON"])
    with tabs[0]:
        df = _fields(f.get("facts") or [])
        a = st.columns(3)
        ents = a[0].multiselect("Entity", sorted(df["entity"].dropna().unique()) if not df.empty else [])
        stats = a[1].multiselect("Status", sorted(df["status"].dropna().unique()) if not df.empty else [])
        q = a[2].text_input("Search value / field")
        if ents:
            df = df[df["entity"].isin(ents)]
        if stats:
            df = df[df["status"].isin(stats)]
        if q:
            df = df[df["value"].str.contains(q, case=False, na=False) | df["field"].str.contains(q, case=False, na=False)]
        st.dataframe(df, hide_index=True, height=560)
        fid = st.selectbox("Fact detail", [""] + [x.get("fact_id") for x in f.get("facts") or []])
        if fid:
            st.json(next(x for x in f["facts"] if x.get("fact_id") == fid), expanded=2)
    with tabs[1]:
        for x in checks:
            icon = OUTCOME_ICON.get(str(x.get("outcome")), "•")
            with st.expander(f"{icon} Rule {x.get('rule')} · {x.get('name')} — {x.get('outcome')}: {x.get('detail') or ''}"):
                st.json(x, expanded=2)
    with tabs[2]:
        for x in f.get("conflicts") or []:
            with st.expander(f"⚠️ {x.get('conflict_id')} · {x.get('title')}", expanded=True):
                st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in ver.items()} for ver in x.get("versions") or []]),
                             hide_index=True)
                st.caption(f"affects {as_text(x.get('affected'))} · review {as_text(x.get('review')) or '—'}")
        if not f.get("conflicts"):
            st.success("No conflicts.")
    with tabs[3]:
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items()} for x in f.get("tmd_sheets") or []]),
                     hide_index=True)
    with tabs[4]:
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items() if k != "source"} for x in f.get("mentions") or []]),
                     hide_index=True, height=520)
    with tabs[5]:
        st.markdown(f"**Approval:** {(f.get('approval') or {}).get('status')}")
        st.json(f.get("approval") or {}, expanded=False)
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items() if k != "sources"} for x in f.get("signoffs") or []]),
                     hide_index=True)
    with tabs[6]:
        review = data.read_json(doc / "review.json") or {}
        items = review.get("items") or f.get("review") or []
        st.caption("Corrections are applied with `tmp-pipeline-review apply <doc_dir> corrections.json`.")
        st.dataframe(pd.DataFrame([{k: as_text(v) for k, v in x.items()} for x in items]), hide_index=True, height=480)
        applied = data.read_jsonl(doc / "corrections.jsonl")
        if applied:
            st.markdown("**Applied corrections**")
            st.dataframe(pd.DataFrame(applied).astype(str), hide_index=True)
    with tabs[7]:
        st.dataframe(pd.DataFrame(f.get("other") or []), hide_index=True, height=480)
    with tabs[8]:
        st.json(f, expanded=1)
