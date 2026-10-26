import json

import pytest

from tmp_pipeline.cache import CacheMiss, FileCache, stable_key
from tmp_pipeline.transport import ServiceError, TransientServiceError, raise_for_status, with_retry


def test_stable_key_ignores_dict_order_and_changes_with_content():
    assert stable_key("a", {"x": 1, "y": 2}) == stable_key("a", {"y": 2, "x": 1})
    assert stable_key("a", {"x": 1}) != stable_key("a", {"x": 2})
    assert stable_key("a", "b") != stable_key("ab")


def test_file_cache_round_trip_and_miss(tmp_path):
    c = FileCache(tmp_path / "c")
    assert not c.has("k")
    with pytest.raises(CacheMiss):
        c.get("k")
    c.put("k", {"n": 1, "text": "Pĝ"})
    assert c.has("k") and c.get("k") == {"n": 1, "text": "Pĝ"}
    assert [p.name for p in (tmp_path / "c").iterdir()] == ["k.json"]        # no temp files left


def test_a_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    c = FileCache(tmp_path)
    c.put("k", {"ok": True})
    c.path("k").write_text('{"truncated": ', encoding="utf-8")
    assert not c.has("k")
    with pytest.raises(CacheMiss):
        c.get("k")
    c.put("k", {"ok": True})                                                 # and it can be rewritten
    assert c.get("k") == {"ok": True}


def _scripted(*responses):
    calls = []
    it = iter(responses)

    def transport(method, url, headers, body):
        calls.append((method, url))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return transport, calls


def test_retry_on_throttle_then_success_with_exponential_backoff():
    t, calls = _scripted((429, {}, b""), (503, {}, b""), (200, {}, b"ok"))
    sleeps = []
    status, _, body = with_retry(t, retries=3, backoff_s=1, sleep=sleeps.append)("GET", "u", {}, None)
    assert (status, body) == (200, b"ok") and len(calls) == 3 and sleeps == [1, 2]


def test_retry_after_header_wins_and_is_capped():
    t, _ = _scripted((429, {"Retry-After": "7"}, b""), (429, {"retry-after": "500"}, b""), (200, {}, b""))
    sleeps = []
    with_retry(t, retries=3, max_backoff_s=30, sleep=sleeps.append)("GET", "u", {}, None)
    assert sleeps == [7, 30]


def test_no_retry_for_client_errors_and_last_response_is_returned_when_retries_run_out():
    t, calls = _scripted((400, {}, b"bad"))
    assert with_retry(t, sleep=lambda s: None)("GET", "u", {}, None)[0] == 400 and len(calls) == 1
    t, calls = _scripted((500, {}, b"a"), (500, {}, b"b"), (500, {}, b"c"))
    assert with_retry(t, retries=2, sleep=lambda s: None)("GET", "u", {}, None) == (500, {}, b"c")


def test_transient_errors_are_retried_and_then_raised():
    t, calls = _scripted(TransientServiceError("net"), (200, {}, b"ok"))
    assert with_retry(t, sleep=lambda s: None)("GET", "u", {}, None)[0] == 200 and len(calls) == 2
    t, _ = _scripted(TransientServiceError("a"), TransientServiceError("b"))
    with pytest.raises(TransientServiceError):
        with_retry(t, retries=1, sleep=lambda s: None)("GET", "u", {}, None)


def test_raise_for_status_maps_statuses_to_typed_errors():
    raise_for_status(202, "x")
    with pytest.raises(TransientServiceError) as e:
        raise_for_status(429, "x", b"slow down")
    assert e.value.status == 429 and "slow down" in str(e.value)
    with pytest.raises(ServiceError) as e2:
        raise_for_status(401, "x")
    assert not isinstance(e2.value, TransientServiceError)


def test_json_payload_survives_cache_unchanged(tmp_path):
    c = FileCache(tmp_path)
    payload = {"a": [1, 2.5, None, {"b": "c"}]}
    c.put("p", payload)
    assert json.loads(c.path("p").read_text(encoding="utf-8")) == payload
