"""Saved question-set results under the output root (``_qa_*.json`` from ``tmp-pipeline-qa --json``) and step timings."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import data
from . import as_text, current_root


def render() -> None:
    root = current_root()
    st.title("QA results")
    files = data.root_files(root, "_qa_*.json")
    timing = root / "_timing_steps.json"
    if not files and not timing.exists():
        st.info(f"No `_qa_*.json` files in `{root}`. Write them with `tmp-pipeline-qa questions.json --json > {root}/_qa_x.json`.")
        return
    tabs = st.tabs(["Results", "Compare sets", "Step timings"])
    with tabs[0]:
        if files:
            _results(files)
    with tabs[1]:
        if files:
            _compare(files)
    with tabs[2]:
        _timing(timing)


def _frame(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        checks = r.get("checks") or {}
        out.append({"qid": r.get("qid"), "question": r.get("question"), "pass": all(checks.values()) if checks else None,
                    "failed_checks": ", ".join(k for k, v in checks.items() if not v), "kind": r.get("kind"),
                    "method": r.get("method"), "types": as_text(r.get("types")), "row": r.get("row"),
                    "repaired": r.get("repaired"), "latency_ms": r.get("latency_ms"), "cost_usd": r.get("cost_usd"),
                    "missing": as_text(r.get("missing")), "problems": as_text(r.get("problems")),
                    "answer": r.get("answer")})
    return pd.DataFrame(out)


def _results(files) -> None:
    f = st.selectbox("Result file", files, format_func=lambda p: p.name)
    rows = data.read_json(f) or []
    df = _frame(rows)
    c = st.columns(5)
    c[0].metric("Questions", len(df))
    c[1].metric("All checks pass", f"{int(df['pass'].fillna(False).sum())}/{len(df)}")
    c[2].metric("Repaired", int(df["repaired"].fillna(False).sum()))
    if df["latency_ms"].notna().any():
        c[3].metric("Latency p95 ms", f"{df['latency_ms'].quantile(0.95):.0f}")
    if df["cost_usd"].notna().any():
        c[4].metric("Cost $", f"{df['cost_usd'].fillna(0).sum():.4f}")
    txt = data.read_text(f.with_suffix(".txt"))
    if txt:
        with st.expander("Score table"):
            st.code(txt, language=None)
    checks = pd.DataFrame([r.get("checks") or {} for r in rows])
    if not checks.empty:
        st.bar_chart(checks.mean().rename("pass rate"), horizontal=True, height=260)
    only_fail = st.toggle("Failures only")
    view = df[df["pass"] == False] if only_fail else df  # noqa: E712
    event = st.dataframe(view, hide_index=True, height=420, on_select="rerun", selection_mode="single-row",
                         key=f"qa_{f.name}")
    if event.selection.rows:
        qid = view.iloc[event.selection.rows[0]]["qid"]
        st.json(next(r for r in rows if r.get("qid") == qid), expanded=2)


def _compare(files) -> None:
    picked = st.multiselect("Sets", files, default=files[:3], format_func=lambda p: p.name)
    rows = []
    for f in picked:
        df = _frame(data.read_json(f) or [])
        rows.append(df.set_index("qid")["pass"].rename(f.stem.removeprefix("_qa_")))
    if rows:
        table = pd.concat(rows, axis=1)
        st.dataframe(table, height=520)
        st.bar_chart(table.fillna(False).astype(int).mean().rename("pass rate"))


def _timing(path) -> None:
    rows = data.read_json(path)
    if not rows:
        st.info("No `_timing_steps.json` in this output root.")
        return
    df = pd.DataFrame([{"id": r.get("id"), "set": r.get("set"), "total": r.get("total"), "ctx": r.get("ctx"),
                        "method": r.get("method"), "repaired": r.get("repaired"), **(r.get("steps") or {})}
                       for r in rows])
    sets = st.multiselect("Set", sorted(df["set"].dropna().unique()), key="tm_set")
    if sets:
        df = df[df["set"].isin(sets)]
    steps = [c for c in ("resolve", "classify", "tools", "context", "answer", "check") if c in df]
    st.markdown("**Mean ms per step**")
    st.bar_chart(df[steps].mean(), horizontal=True, height=240)
    st.markdown("**Per question**")
    st.bar_chart(df.set_index("id")[steps], height=360)
    st.dataframe(df, hide_index=True)
