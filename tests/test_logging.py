import json
import re

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
