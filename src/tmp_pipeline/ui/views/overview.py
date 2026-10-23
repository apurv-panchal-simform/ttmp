"""Document metadata, versions, stage timings, coverage, lineage, failure and report.md."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data
from . import as_text, current_doc


def render() -> None:
    doc = current_doc()
    els = data.elements(doc)
    meta = els.get("metadata", {})
    st.title(meta.get("tmp_reference") or doc.name)
    st.caption(f"`{els.get('doc_id', doc.name)}` · {doc}")

    failed = data.read_json(doc / "FAILED")
    if failed:
        st.error(f"Run failed in stage **{failed.get('stage')}**: {failed.get('error')}")
        with st.expander("Traceback"):
            st.code(failed.get("trace", ""), language="python")

    f = data.facts(doc)
    _, chunk_rows = data.chunks(doc)
    c = st.columns(6)
    c[0].metric("Pages", meta.get("pages", "–"))
    c[1].metric("Elements", len(els.get("elements", [])))
    c[2].metric("Records", len(data.records(doc)))
    c[3].metric("Facts", len(f.get("facts", [])))
    c[4].metric("Chunks", len(chunk_rows))
    c[5].metric("Approval", (f.get("approval") or {}).get("status", "–"))

    left, right = st.columns(2)
    with left:
        st.subheader("Metadata")
        st.dataframe(pd.DataFrame([{"key": k, "value": as_text(v)} for k, v in meta.items()
                                   if k not in ("versions", "form_names")]),
                     hide_index=True, width="stretch")
        st.subheader("Versions")
        st.json(meta.get("versions") or f.get("versions") or {}, expanded=False)
    with right:
        st.subheader("Stage timings (latest run)")
        t = data.stage_timings(data.read_jsonl(doc / "run.log"))
        if t.empty:
            st.caption("no run.log")
        else:
            st.bar_chart(t, x="stage", y="duration_ms", horizontal=True)
            st.caption(f"total {t['duration_ms'].sum() / 1000:.1f} s")
        st.subheader("Coverage & lineage")
        st.json({"coverage": f.get("coverage"), "lineage": data.read_json(doc / "lineage.json")}, expanded=False)

    st.subheader("Pages")
    pages = els.get("pages") or {}
    st.dataframe(pd.DataFrame([{"page": int(p), "zone": v.get("zone"), "zone_reason": v.get("zone_reason"),
                                "form": v.get("form_name"), "printed": v.get("printed_page"),
                                "chars": v.get("n_chars"), "paths": v.get("n_paths"),
                                "text_quality": v.get("text_quality")} for p, v in pages.items()]),
                 hide_index=True, width="stretch")

    st.subheader("Files")
    st.dataframe(pd.DataFrame([{"file": n, "size_kb": round((doc / n).stat().st_size / 1024, 1)}
                               for n in data.DOC_FILES if (doc / n).exists()]), hide_index=True)

    report = data.read_text(doc / "report.md")
    if report:
        with st.expander("report.md", expanded=False):
            st.markdown(report)
