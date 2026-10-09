import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from tmp_pipeline import logging_ as lg


def test_every_line_has_run_and_doc_id(tmp_path):
    path = tmp_path / "run.log"
    run_id = lg.start_run(path)
    lg.set_doc_id("sha256:abc")
    lg.log_event("intake", "start", file="x.pdf", pages=25)
    lg.log_event("layers", "page", page=1, chars=10)
    lg.end_run()
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 2
    for rec in lines:
        assert rec["run_id"] == run_id
        assert rec["doc_id"] == "sha256:abc"
        assert {"stage", "event", "ts"} <= set(rec)
    assert re.fullmatch(r"r-\d{8}-\d{6}-[0-9a-f]{4}", run_id)


def test_doc_id_is_empty_string_before_it_is_known(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    lg.log_event("intake", "hash")
    lg.end_run()
    assert json.loads(path.read_text())["doc_id"] == ""


def test_secrets_are_redacted(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    lg.log_event("di", "request", api_key="SECRET", key="SECRET2", endpoint="https://x")
    lg.end_run()
    text = path.read_text()
    assert "SECRET" not in text
    assert json.loads(text)["endpoint"] == "https://x"


def test_log_event_without_run_is_a_noop():
    lg.end_run()
    lg.log_event("intake", "ignored")  # must not raise


def test_nested_secrets_are_redacted(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    lg.log_event("di", "request", headers={"Authorization": "Bearer SECRET", "accept": "json"},
                 calls=[{"api_key": "SECRET2"}])
    lg.end_run()
    text = path.read_text()
    assert "SECRET" not in text
    assert json.loads(text)["headers"]["accept"] == "json"


def test_levels_and_exception_logging(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    lg.log_warning("x", "careful")
    try:
        raise ValueError("bad value")
    except ValueError as e:
        lg.log_exception("x", "failed", e, page=3)
    lg.end_run()
    warn, err = (json.loads(x) for x in path.read_text().splitlines())
    assert warn["level"] == "warning"
    assert err["level"] == "error" and err["error"] == "ValueError: bad value" and "Traceback" in err["trace"]
    assert err["page"] == 3


def test_stage_logs_start_finish_and_failure_then_reraises(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    with lg.stage("layers", pages=2):
        pass
    with pytest.raises(RuntimeError), lg.stage("zones"):
        raise RuntimeError("nope")
    lg.end_run()
    events = [(r["stage"], r["event"]) for r in map(json.loads, path.read_text().splitlines())]
    assert events == [("layers", "stage_started"), ("layers", "stage_finished"),
                      ("zones", "stage_started"), ("zones", "stage_failed")]


def test_worker_threads_keep_run_and_doc_id_only_through_submit_in_context(tmp_path):
    path = tmp_path / "run.log"
    run_id = lg.start_run(path)
    lg.set_doc_id("sha256:abc")
    with ThreadPoolExecutor(2) as pool:
        lg.submit_in_context(pool, lg.log_event, "w", "carried").result()
        pool.submit(lg.log_event, "w", "lost").result()          # a plain submit loses the context: no line
    lg.end_run()
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [(r["event"], r["run_id"], r["doc_id"]) for r in lines] == [("carried", run_id, "sha256:abc")]


def test_concurrent_writers_never_interleave_a_line(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    with ThreadPoolExecutor(8) as pool:
        futs = [lg.submit_in_context(pool, lg.log_event, "w", "e", n=i, pad="x" * 2000) for i in range(200)]
        for f in futs:
            f.result()
    lg.end_run()
    lines = path.read_text().splitlines()
    assert len(lines) == 200 and all(json.loads(x)["event"] == "e" for x in lines)
