"""Upload a PDF and run the whole pipeline in-process (as ``tmp-pipeline``), with live stage progress from run.log."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import streamlit as st

from ...intake import compute_doc_id, doc_dir
from ...model.embeddings import ReplayEmbeddings
from ...model.live import MODES, model_client
from ...run import RunResult, run
from .. import data
from . import current_root


@dataclass
class Job:
    pdf: Path
    out_root: Path
    log: Path
    start_line: int
    thread: threading.Thread | None = None
    result: RunResult | None = None
    error: BaseException | None = None
    started: float = field(default_factory=time.time)

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def events(self) -> list[dict]:
        return data.read_jsonl(self.log)[self.start_line:] if self.log.exists() else []


def _start(pdf: Path, out_root: Path, opts: dict) -> Job:
    log = doc_dir(out_root, compute_doc_id(pdf)) / "run.log"
    start = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
    job = Job(pdf, out_root, log, start)

    def work() -> None:
        try:
            cache = out_root / "_model_cache" if opts["model"] == "live" else None
            model = model_client(opts["model"], opts["model_fixtures"], cache)
            embedder = ReplayEmbeddings(opts["embed_fixtures"]) if opts["embed"] == "replay" else None
            job.result = run(pdf, out_root, opts["template"], opts["force"], not opts["skip_render"],
                             model=model, gold=opts["gold"], embedder=embedder)
        except BaseException as e:      # shown on the page; the run itself also writes FAILED
            job.error = e

    job.thread = threading.Thread(target=work, name="tmp-pipeline-run", daemon=True)
    job.thread.start()
    return job


def _progress(job: Job) -> pd.DataFrame:
    rows: dict[str, dict] = {}
    for e in job.events():
        stage, ev = str(e.get("stage")), e.get("event")
        if ev == "stage_started":
            rows[stage] = {"stage": stage, "status": "running", "ms": None}
        elif ev == "stage_finished":
            rows[stage] = {"stage": stage, "status": "done", "ms": e.get("duration_ms")}
        elif ev == "stage_failed":
            rows[stage] = {"stage": stage, "status": "failed", "ms": e.get("duration_ms")}
    return pd.DataFrame(list(rows.values()))


def _form(root: Path) -> tuple[Path | None, dict]:
    up = st.file_uploader("TMP PDF", type=["pdf"])
    local = st.text_input("…or a PDF path on this machine", placeholder="tests/data/sample.pdf")
    c = st.columns(3)
    out_root = Path(c[0].text_input("Output root", value=str(root)))
    force = c[1].checkbox("Force re-run", help="re-run even if this file was already processed")
    skip_render = c[2].checkbox("Skip page renders")
    c = st.columns(2)
    model = c[0].selectbox("Model", MODES, help="off: no model calls · replay: recorded · live: Azure OpenAI")
    model_fixtures = c[1].text_input("Model fixtures folder", disabled=model == "off") or None
    c = st.columns(2)
    embed = c[0].selectbox("Embeddings", ["off", "replay"])
    embed_fixtures = c[1].text_input("Embedding fixtures folder", disabled=embed == "off") or None
    c = st.columns(2)
    template = c[0].text_input("Template TOML (optional)") or None
    gold = c[1].text_input("Gold JSON (optional)", placeholder="tests/data/gold/sample.gold.json") or None
    pdf: Path | None = None
    if up is not None:
        pdf = out_root / "_uploads" / Path(up.name).name
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(up.getbuffer())
    elif local:
        pdf = Path(local)
    return pdf, {"out_root": out_root, "force": force, "skip_render": skip_render, "model": model,
                 "model_fixtures": model_fixtures, "embed": embed, "embed_fixtures": embed_fixtures,
                 "template": template, "gold": gold}


def _problem(pdf: Path | None, o: dict) -> str | None:
    if pdf is None:
        return "Upload a PDF or give a path."
    if not pdf.exists():
        return f"{pdf} does not exist."
    if o["model"] == "replay" and not o["model_fixtures"]:
        return "Model replay needs a fixtures folder."
    if o["embed"] == "replay" and not o["embed_fixtures"]:
        return "Embedding replay needs a fixtures folder."
    return None


def render() -> None:
    st.title("Run pipeline")
    job: Job | None = st.session_state.get("run_job")
    if job is None or not job.running:
        pdf, opts = _form(current_root())
        if st.button("Run", type="primary"):
            problem = _problem(pdf, opts)
            if problem or pdf is None:
                st.error(problem)
            else:
                st.session_state["run_job"] = job = _start(pdf, opts["out_root"], opts)
    if job is None:
        return

    st.divider()
    st.subheader(f"{job.pdf.name}")
    table = st.empty()
    with st.status("Running…" if job.running else "Finished", expanded=True) as status:
        while job.running:
            table.dataframe(_progress(job), hide_index=True)
            status.update(label=f"Running… {time.time() - job.started:.0f} s")
            time.sleep(1)
        table.dataframe(_progress(job), hide_index=True)
        if job.error is not None:
            status.update(label="Failed", state="error")
            st.error(f"{type(job.error).__name__}: {job.error}")
            failed = data.read_json(doc_dir(job.out_root, compute_doc_id(job.pdf)) / "FAILED")
            if failed:
                st.code(failed.get("trace", ""), language="python")
            return
        res = job.result
        if res is None:
            return
        if res.skipped:
            status.update(label="Already processed", state="complete")
            st.info(f"Already processed: {res.out_dir}. Tick **Force re-run** to run again.")
        else:
            status.update(label=f"Done in {time.time() - job.started:.0f} s", state="complete")
            if res.router is not None and res.store is not None:
                cov = res.router.coverage(res.store)
                c = st.columns(4)
                c[0].metric("Elements", len(res.store.elements))
                c[1].metric("Coverage", f"{cov['coverage']:.0%}")
                c[2].metric("Failed handlers", cov["failed"])
                c[3].metric("Chunks", len(res.chunks.chunks) if res.chunks else 0)
    if st.button("Open this document", type="primary"):
        from ..nav import open_document
        st.session_state.pop("run_job", None)
        open_document(str(job.out_root), str(res.out_dir))
