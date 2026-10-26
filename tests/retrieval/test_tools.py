# ruff: noqa: F811
"""The code tools the answer model can call (calculate, time_check, find_tmps ...) and the answer loop around them,
with a scripted model: tool calls run in code, their results join the context, and the answer check accepts what they
computed. No live calls."""
import datetime as dt
import json

import pytest

from test_retrieval import CFG, NOW, _retriever, _session, built, store, two  # noqa: F401
from tmp_pipeline.calc import calculate
from tmp_pipeline.llm import FakeClient
from tmp_pipeline.retrieval.search import LocalSearch
from tmp_pipeline.retrieval.session import Session
from tmp_pipeline.retrieval.store import User
from tmp_pipeline.retrieval.timecheck import Calendar, load_calendar
from tmp_pipeline.retrieval.tools import ToolEnv, run_tool

CAL = load_calendar("nz_calendar.toml")
V = {"rp_from": {"value": 0.465, "unit": "km", "source": "p.18"}, "rp_to": {"value": 0.615, "unit": "km", "source": "p.18"}}


# --- calculate and the calendar ----------------------------------------------------------------------------------

def test_calculate_shows_the_working_and_rejects_numbers_of_its_own():
    out = calculate("rp_to - rp_from", V, "m", {"value": 150, "unit": "m", "source": "p.18"})
    assert out["result"] == 150 and out["working"] == "0.615 km − 0.465 km = 0.150 km = 150 m"
    assert out["cross_check"] == "matches the stated 150 m (p.18)"
    assert "numbers may not be written" in calculate("rp_to - 0.4", V)["error"]
    assert "not one of the values" in calculate("rp_to - other", V)["error"]


@pytest.mark.parametrize("day,want", [("2026-10-14", "school_day"), ("2026-10-05", "school_holiday"),
                                      ("2026-10-03", "weekend"), ("2026-10-26", "public_holiday"), ("2028-03-01", None)])
def test_the_calendar_turns_a_date_into_a_day_type(day, want):
    assert CAL.day_type(dt.date.fromisoformat(day))[0] == want


def test_uncertain_calendar_days_give_no_day_type():
    cal = Calendar((dt.date(2026, 1, 1), dt.date(2026, 12, 31)), ((dt.date(2026, 2, 9), dt.date(2026, 4, 2)),),
                   ((dt.date(2026, 1, 26), dt.date(2026, 2, 8)),))
    assert cal.day_type(dt.date(2026, 2, 3))[0] is None and cal.day_type(dt.date(2026, 2, 10))[0] == "school_day"


# --- the tools over a TMP ----------------------------------------------------------------------------------------

def _env(store, doc=True):
    return ToolEnv(store, LocalSearch(store, CFG), CFG, User(), store.docs(User())[0] if doc else None, NOW, CAL)


def test_calculate_over_an_entitys_own_fields(store):
    out = run_tool("calculate", {"entity": "speed_limit", "expression": "rp_to - rp_from", "result_unit": "m"}, _env(store))
    (res,) = out["results"]
    assert res["result"] == 150 and res["working"].endswith("= 150 m") and "page 18" in res["sources"][0]
    bad = run_tool("calculate", {"entity": "speed_limit", "expression": "rp_to - 0.4"}, _env(store))
    assert "numbers may not be written" in bad["results"][0]["error"]


def test_time_check_at_4pm_on_a_school_day_is_not_allowed(store):
    out = run_tool("time_check", {"what": "work", "date": "2026-10-14", "time": "16:00"}, _env(store))
    assert out["allowed"] is False and out["reason"] == "16:00 is after the 15:00 site-cleared time"
    assert run_tool("time_check", {"what": "work", "date": "2026-10-14", "time": "10:00"}, _env(store))["allowed"] is True


def test_get_facts_and_a_bad_call_never_raise(store):
    out = run_tool("get_facts", {"entity": "speed_limit"}, _env(store))
    assert out["items"][0]["values"]["rp_to"]["value"] == 0.615
    assert "unknown tool" in run_tool("nope", {}, _env(store))["error"]
    assert "bad arguments" in run_tool("get_facts", {"entity": "road", "wrong": 1}, _env(store))["error"]
    assert "no TMP is chosen" in run_tool("get_facts", {"entity": "road"}, _env(store, doc=False))["error"]


def test_find_tmps_lists_readable_tmps_and_a_tmp_can_be_named(two):
    out = run_tool("find_tmps", {"road": "Sunrise"}, _env(two, doc=False))
    assert out["count"] == 2
    named = run_tool("get_facts", {"entity": "tmp_document", "tmp": "KTS 26-1700"}, _env(two, doc=False))
    assert named["tmp"].startswith("KTS 26-1700")


def test_derived_figures_are_written_into_the_chunks(built):
    _, cs = built
    text = "\n".join(c.content for c in cs.chunks)
    assert "Length from the RPs: 0.615 km − 0.465 km = 0.150 km = 150 m" in text


# --- the answer loop ---------------------------------------------------------------------------------------------

def _calc_call():
    return {"tool_calls": [{"id": "c1", "name": "calculate", "arguments": {
        "entity": "speed_limit", "expression": "rp_to - rp_from", "result_unit": "m"}}]}


def test_a_tool_result_joins_the_context_and_its_numbers_pass_the_check(store):
    def q3(req):
        if not req.thread:
            return _calc_call()
        assert json.loads(req.thread[-1])["role"] == "tool"
        return {"answer": "The 30 km/h zone is 150 m long: 0.615 km − 0.465 km = 0.150 km = 150 m (page 18)."}
    model = FakeClient({"Q3": q3})
    res = _retriever(store, model).ask("How long is the zone, worked out from the RPs?", _session(store))
    assert res.method == "Q3" and res.problems == [] and not res.repaired
    assert [c["tool"] for c in res.tool_calls] == ["calculate"] and len(model.calls) == 2
    assert "TOOL RESULTS" in res.context.text and 18 in res.context.pages


def test_a_question_the_chunks_answer_makes_one_call(store):
    model = FakeClient({"Q3": {"answer": "The temporary limit is 30 km/h (page 18)."}})
    res = _retriever(store, model).ask("What is the temporary speed limit?", _session(store))
    assert res.tool_calls == [] and len(model.calls) == 1 and model.calls[0].tools


def test_the_loop_stops_at_its_limits_and_falls_back_to_the_safe_answer(store):
    model = FakeClient({"Q3": lambda req: _calc_call()})          # asks for a tool every time, even when none is offered
    res = _retriever(store, model).ask("How long is the zone?", _session(store))
    assert len(model.calls) == CFG.retrieval.max_tool_rounds + 1 and not model.calls[-1].tools
    assert res.method.startswith("safe") and res.problems == []


def test_a_number_nothing_gave_goes_back_once_with_a_tool_hint(store):
    def q3(req):
        if req.thread:
            return {"answer": "The zone is 150 m (page 18)."}
        if "not in the context" in req.user:
            return _calc_call()
        return {"answer": "The zone is 987 m (page 18)."}          # a number the context does not hold
    model = FakeClient({"Q3": q3})
    res = _retriever(store, model).ask("How long is the zone?", _session(store))
    assert res.method == "Q3" and res.problems == [] and [c["tool"] for c in res.tool_calls] == ["calculate"]
    assert len(model.calls) == 3 and "987" in model.calls[1].user


def test_with_tools_off_the_model_is_offered_none(store):
    cfg = CFG.__class__(**{**CFG.__dict__, "retrieval": CFG.retrieval.__class__(**{**CFG.retrieval.__dict__, "tools": False})})
    model = FakeClient({"Q3": {"answer": "The temporary limit is 30 km/h (page 18)."}})
    _retriever(store, model).__class__(store, LocalSearch(store, cfg), cfg, model, lambda: NOW).ask(
        "What is the speed limit?", _session(store))
    assert not model.calls[0].tools


# --- several TMPs, none chosen -----------------------------------------------------------------------------------

def test_library_mode_answers_across_tmps_with_find_tmps(two):
    def q3(req):
        if not req.thread:
            return {"tool_calls": [{"id": "f1", "name": "find_tmps", "arguments": {"road": "Sunrise"}}]}
        return {"answer": "Both KTS 26-1581 V1 and KTS 26-1700 V1 work on Sunrise Avenue."}
    model = FakeClient({"Q3": q3})
    res = _retriever(two, model).ask("Which TMPs are on Sunrise Avenue?", Session())
    assert res.kind == "answer" and res.resolved_via == "all TMPs" and res.tool_calls[0]["tool"] == "find_tmps"


def test_library_mode_asks_back_when_the_question_needs_one_tmp(two):
    model = FakeClient({"Q3": {"ask_back": True}})
    res = _retriever(two, model).ask("Is the work approved?", Session())
    assert res.kind == "ask_back" and res.answer.startswith("I found 2 TMPs:")
