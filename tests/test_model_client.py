import json

import pytest

from tmp_pipeline import logging_ as lg
from tmp_pipeline.cache import FileCache
from tmp_pipeline.llm import (
    CachedClient,
    DisabledClient,
    FakeClient,
    ModelRequest,
    ModelResponse,
    ModelUnavailableError,
    RecordingClient,
    ReplayClient,
    ask,
)


def req(**kw):
    base = dict(prompt_id="X2", prompt_version="1", model="gpt-5.4", user="Names: Road, AADT")
    return ModelRequest(**{**base, **kw})


def test_request_key_changes_with_every_ingredient():
    base = req().key()
    assert base == req().key()
    for other in (req(prompt_id="X4"), req(prompt_version="2"), req(model="gpt-6-sol"), req(user="other"),
                  req(system="s"), req(images=(b"png",)), req(params={"temperature": 0})):
        assert other.key() != base


def test_fake_client_answers_by_prompt_and_records_calls():
    fake = FakeClient({"X2": {"entity": "road"}, "X4": lambda r: {"echo": r.user}})
    assert fake.complete(req()).data == {"entity": "road"}
    assert fake.complete(req(prompt_id="X4", user="hi")).data == {"echo": "hi"}
    assert [c.prompt_id for c in fake.calls] == ["X2", "X4"]
    with pytest.raises(ModelUnavailableError):
        fake.complete(req(prompt_id="X9"))


def test_cached_client_calls_inner_once_per_distinct_request(tmp_path):
    fake = FakeClient({"X2": {"entity": "road"}})
    c = CachedClient(fake, FileCache(tmp_path))
    r1, r2 = c.complete(req()), c.complete(req())
    assert not r1.cached and r2.cached and r2.data == {"entity": "road"} and len(fake.calls) == 1
    c.complete(req(prompt_version="2"))                                   # new prompt version -> miss
    assert len(fake.calls) == 2


def test_record_then_replay_offline_and_unrecorded_raises(tmp_path):
    rec = RecordingClient(FakeClient({"X2": {"entity": "road"}}), tmp_path)
    rec.complete(req())
    replay = ReplayClient(tmp_path)
    assert replay.complete(req()).data == {"entity": "road"}
    with pytest.raises(ModelUnavailableError, match="nothing recorded"):
        replay.complete(req(user="never asked"))


def test_ask_returns_none_and_logs_when_there_is_no_model(tmp_path):
    path = tmp_path / "run.log"
    lg.start_run(path)
    assert ask(None, req()) is None
    assert ask(DisabledClient(), req()) is None
    assert ask(FakeClient({"X2": {"entity": "road"}}), req()) == {"entity": "road"}
    lg.end_run()
    events = [json.loads(x)["event"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert events == ["model_skipped", "model_skipped"]


def test_response_type_defaults():
    r = ModelResponse(data={"a": 1})
    assert r.cached is False and r.usage == {}
