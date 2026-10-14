"""Retrieval Level 0 + 1 (plan part 5) on hand-built TMPs: details, rules, resolve, calculate, time_check and the
calendar, hybrid search, context budget, the answer check, Q1/Q3/Q4 through a fake model, and the live chat client
against a fake transport."""
import copy
import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from test_chunks import _chunks
from tmp_pipeline.config import load_config
from tmp_pipeline.facts.model import FactValue
from tmp_pipeline.model import prompts
from tmp_pipeline.model.azure_chat import AzureChat, ChatConfig, messages
from tmp_pipeline.model.client import FakeClient, ModelRequest, ModelUnavailableError
from tmp_pipeline.model.embeddings import FakeEmbeddings
from tmp_pipeline.model.live import model_client
from tmp_pipeline.model.validators import check_q1
from tmp_pipeline.records import Status
from tmp_pipeline.retrieval import details as D
from tmp_pipeline.retrieval.answer import safe_answer
from tmp_pipeline.retrieval.calc import calculate
from tmp_pipeline.retrieval.check import check_answer
from tmp_pipeline.retrieval.classify import Reading, classify, rule_types
from tmp_pipeline.retrieval.context import assemble
from tmp_pipeline.retrieval.hybrid import Hit, LocalSearch, SearchRequest, code_key, rank_and_expand, tokens
from tmp_pipeline.retrieval.pipeline import Retriever
from tmp_pipeline.retrieval.recipes import Evidence, Tools, run_recipes
from tmp_pipeline.retrieval.resolve import Session, resolve_tmp
from tmp_pipeline.retrieval.store import LocalStore, User, latest
from tmp_pipeline.retrieval.timecheck import Calendar, load_calendar, time_check
from tmp_pipeline.retrieval.tools import Citer, find_sheet, get_facts, get_status, get_tmd, pages_text
from tmp_pipeline.transport import ServiceError

CFG = load_config()
R = CFG.retrieval
NOW = dt.datetime(2026, 10, 13, 9, 0, tzinfo=ZoneInfo("Pacific/Auckland"))     # a Tuesday in Term 4
TODAY = NOW.date()


def _write(root, facts, chunks, name, groups=None):
    d = root / name
    d.mkdir(parents=True)
    if groups is not None:
        chunks = copy.deepcopy(chunks)                  # the module fixture is shared: tag a copy
        for c in chunks.chunks:
            c.metadata["access_groups"] = list(groups)
    facts.dump(d / "facts.json")
    chunks.dump_jsonl(d / "chunks.jsonl")
    return d


def _other(facts, chunks, ref, doc_id, site):
    """A second TMP on the same road: another reference and doc_id, otherwise the same."""
    f, cs = copy.deepcopy(facts), copy.deepcopy(chunks)
    f.doc_id = cs.doc_id = doc_id
    doc = f.of("tmp_document")[0]
    doc.fields["tmp_reference"] = FactValue(ref, ref, status=Status.VERIFIED)
    f.conflicts = [c for c in f.conflicts if c.rule != 1]
    for c in cs.chunks:
        c.metadata.update(doc_id=doc_id, tmp_ref=ref, tmp_ref_aliases=[], site_address=site)
    return f, cs


@pytest.fixture(scope="module")
def built():
    cs, _, facts, _, _ = _chunks()
    return facts, cs


@pytest.fixture
def store(tmp_path, built):
    facts, cs = built
    _write(tmp_path, facts, cs, "a")
    return LocalStore(tmp_path)


@pytest.fixture
def two(tmp_path, built):
    facts, cs = built
    _write(tmp_path, facts, cs, "a")
    _write(tmp_path, *_other(facts, cs, "KTS 26-1700", "sha256:other", "65 Sunrise Ave, Murrays Bay"), "b")
    return LocalStore(tmp_path)


def _retriever(store, model=None, embedder=None):
    return Retriever(store, LocalSearch(store, CFG, embedder), CFG, model, lambda: NOW)


def _session(store):
    s = Session()
    s.use(store.docs(User())[0])
    return s


# --- details ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [("at 4 pm", "16:00"), ("4.30pm", "16:30"), ("1430 hrs", "14:30"),
                                       ("16:00", "16:00"), ("at 1500", "15:00"), ("call 0800 630 7200", None),
                                       ("RP 0.465", None), ("on 2026-10-14", None), ("in 2026", None)])
def test_times_are_read_from_a_question_but_phones_rps_and_years_are_not(text, want):
    assert D.parse_time(text) == want


@pytest.mark.parametrize("text,want", [("Wednesday", "2026-10-14"), ("next Tuesday", "2026-10-20"),
                                       ("14 October", "2026-10-14"), ("Oct 14", "2026-10-14"),
                                       ("tomorrow", "2026-10-14"), ("28/10/2026", "2026-10-28"),
                                       ("5 Jan", "2027-01-05"), ("no date here", None)])
def test_dates_are_resolved_against_nz_today(text, want):
    got = D.parse_date(text, TODAY)
    assert (got.isoformat() if got else None) == want


def test_code_details_and_tmp_references_are_canonical():
    d = D.code_details("Can we work at 4 pm on a school day under kts26 - 1580 near TMD4 and RP 0.465?", R, TODAY)
    assert d == {"tmp_ref": "KTS 26-1580", "time": "16:00", "day_type": "school_day", "tmd": "TMD4", "rp": "0.465"}


# --- rules (plan 5.4) --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,types", [
    ("Is this TMP approved?", ["status"]), ("Who signed the TMP?", ["status"]),
    ("Does another network's RCA need to sign?", ["status"]),
    ("How long is the 30 km/h zone?", ["calculation"]), ("What is the distance between the RPs?", ["calculation"]),
    ("Can we work at 4 pm on a school day?", ["time_check"]), ("Is the TSL in force on Saturday?", ["time_check"]),
    ("Which diagram is TMD4?", ["diagram"]), ("What signs does a westbound driver pass?", ["diagram"]),
    ("What's the taper length on the Stop/Go layout?", ["diagram"]),
    ("Which TMPs on Sunrise Ave are active next week?", ["across_tmps"]),
    ("What happens if there's a spill?", ["procedure"]), ("Who do we call if work runs late?", ["procedure"]),
    ("Give me a toolbox summary", ["summary"]), ("What changed in V2?", ["version_change"]),
    ("Posted speed on Gull Lane?", ["fact"]), ("Who is the client?", ["fact"]),
    ("Is a 30 m taper enough for a 50 km/h road?", ["diagram", "out_of_scope"]),
    ("and weekends?", []),
])
def test_rules_read_each_question_type(question, types):
    assert rule_types(question, R) == types


def test_one_rule_reading_skips_q1_and_several_readings_ask_it():
    model = FakeClient({"Q1": {"standalone_question": "Is a 30 m taper sufficient on a 50 km/h road?",
                               "types": ["out_of_scope", "diagram", "bogus"], "confidence": "medium",
                               "details": {"tmd": None, "road": "Sunrise Avenue", "colour": "red"}}})
    r = classify("Is this TMP approved?", None, R, model, NOW)
    assert (r.types, r.method) == (["status"], "rules") and not model.calls
    r = classify("Is a 30 m taper enough for a 50 km/h road?", None, R, model, NOW)
    assert r.method == "Q1" and r.types == ["out_of_scope", "diagram"]
    assert r.details == {"road": "Sunrise Avenue"} and "bogus" in r.problems[0]
    assert model.calls[0].prompt_id == "Q1" and "Today (NZ time): 2026-10-13 Tuesday" in model.calls[0].user


def test_follow_up_goes_to_q1_and_without_a_model_reuses_the_last_turn():
    s = Session(doc_id="d", tmp_ref="KTS 26-1581", types=["fact"], turns=["Q: What are the school day hours?", "A: ..."])
    model = FakeClient({"Q1": {"standalone_question": "What are the weekend work hours for this TMP?",
                               "types": ["fact"], "confidence": "high",
                               "details": {"entity": "shift_window", "day_type": "weekend"}}})
    r = classify("and weekends?", s, R, model, NOW)
    assert r.method == "Q1" and r.standalone.startswith("What are the weekend") and r.details["entity"] == "shift_window"
    assert "What are the school day hours?" in model.calls[0].user
    r = classify("and weekends?", s, R, None, NOW)
    assert (r.types, r.method, r.details["day_type"]) == (["fact"], "follow-up (no model)", "weekend")


def test_low_confidence_and_no_reading_go_to_the_baseline():
    model = FakeClient({"Q1": {"standalone_question": "hm", "types": ["fact"], "confidence": "low"}})
    assert classify("tell me something", None, R, model, NOW).baseline
    assert classify("tell me something", None, R, None, NOW).method == "baseline"


def test_check_q1_rejects_non_answers():
    assert check_q1(None, "q", prompts.QUESTION_TYPES, prompts.Q1_DETAILS) is None
    c = check_q1({"types": [], "confidence": "high"}, "q", prompts.QUESTION_TYPES, prompts.Q1_DETAILS)
    assert c.confidence == "low" and c.standalone_question == "q"


# --- resolve (plan 5.3) ------------------------------------------------------------------------------------------

def test_resolve_by_reference_alias_address_and_session(two):
    u = User()
    one = resolve_tmp(two, u, CFG, "Is KTS 26-1581 approved?", {}, None)
    assert (one.status, one.via, one.doc.tmp_ref) == ("one", "tmp_ref", "KTS 26-1581")
    alias = resolve_tmp(two, u, CFG, "Is KTS 26-1580 approved?", {}, None)
    assert (alias.status, alias.via, alias.doc.tmp_ref) == ("one", "alias", "KTS 26-1581")
    several = resolve_tmp(two, u, CFG, "Is the work on Sunrise Ave approved?", {}, None)
    assert several.status == "several" and len(several.candidates) == 2
    house = resolve_tmp(two, u, CFG, "Is 65 Sunrise Ave approved?", {}, None)
    assert (house.status, house.via, house.doc.tmp_ref) == ("one", "address", "KTS 26-1700")
    s = Session()
    s.use(alias.doc)
    kept = resolve_tmp(two, u, CFG, "What is the speed on Sunrise Avenue?", {}, s)
    assert (kept.status, kept.via, kept.doc.doc_id) == ("one", "session", alias.doc.doc_id)
    assert resolve_tmp(two, u, CFG, "Is KTS 99-9999 approved?", {}, None).status == "no_match"
    assert resolve_tmp(two, u, CFG, "Is it approved?", {}, None).status == "none"


def test_a_tmp_the_user_may_not_read_is_never_described(tmp_path, built):
    facts, cs = built
    _write(tmp_path, facts, cs, "a", groups=["designers"])
    store = LocalStore(tmp_path)
    assert store.docs(User()) == [] and len(store.docs(User(("designers",)))) == 1
    assert resolve_tmp(store, User(), CFG, "Is KTS 26-1581 approved?", {}, None).status == "no_access"
    res = _retriever(store).ask("Is KTS 26-1581 approved?", Session(), User())
    assert (res.kind, res.answer) == ("no_access", "That TMP isn't available to you.")
    res = _retriever(store).ask("Is KTS 26-1581 approved?", Session(), User(("designers",)))
    assert res.kind == "answer" and "partially signed" in res.answer


def test_latest_version_wins():
    from tmp_pipeline.retrieval.store import DocInfo
    docs = [DocInfo("a", "KTS 1", version=1), DocInfo("b", "KTS 1", version=2), DocInfo("c", "KTS 2")]
    assert {d.doc_id for d in latest(docs)} == {"b", "c"}


def test_ask_back_lists_the_candidates(two):
    res = _retriever(two).ask("Is the work on Sunrise Ave approved?", Session())
    assert res.kind == "ask_back" and res.answer.startswith("I found 2 TMPs:") and "KTS 26-1700" in res.answer


# --- calculate (plan 5.8) ----------------------------------------------------------------------------------------

V = {"rp_from": {"value": 0.465, "unit": "km", "source": "p.18"}, "rp_to": {"value": 0.615, "unit": "km", "source": "p.18"}}


def test_calculate_shows_the_working_and_the_cross_check():
    out = calculate("rp_to - rp_from", V, "m", {"value": 150, "unit": "m", "source": "p.18"})
    assert out["result"] == 150 and out["unit"] == "m" and out["sources"] == ["p.18"]
    assert out["working"] == "0.615 km − 0.465 km = 0.150 km = 150 m"
    assert out["cross_check"] == "matches the stated 150 m (p.18)"
    assert "differs" in calculate("rp_to - rp_from", V, "m", {"value": 160, "unit": "m"})["cross_check"]


@pytest.mark.parametrize("expr,unit,want", [
    ("rp_to > rp_from", None, True), ("max(rp_to, rp_from)", "m", 615), ("abs(rp_from - rp_to)", "m", 150),
    ("rp_to / rp_from", None, 1.322581)])
def test_calculate_operations(expr, unit, want):
    assert calculate(expr, V, unit)["result"] == want


def test_calculate_dates_and_rejected_expressions():
    days = {"s": {"value": "2026-09-28", "unit": "date"}, "e": {"value": "2026-10-28", "unit": "date"}}
    assert calculate("e - s", days, "days")["working"] == "2026-10-28 − 2026-09-28 = 30 days"
    assert "numbers may not be written" in calculate("rp_to - 0.4", V)["error"]
    assert "not one of the values" in calculate("rp_to - other", V)["error"]
    assert "not allowed" in calculate("__import__('os')", V)["error"]
    mixed = {"a": {"value": 1, "unit": "km"}, "b": {"value": 5, "unit": "min"}}
    assert "cannot convert" in calculate("a - b", mixed)["error"]
    assert "cannot convert" in calculate("rp_to - rp_from", V, "h")["error"]


# --- time_check and the calendar (plan 5.8) ----------------------------------------------------------------------

CAL = load_calendar("nz_calendar.toml")


@pytest.mark.parametrize("day,want", [("2026-10-14", "school_day"), ("2026-10-05", "school_holiday"),
                                      ("2026-10-03", "weekend"), ("2026-10-26", "public_holiday"),
                                      ("2026-12-10", None), ("2028-03-01", None)])
def test_calendar_turns_a_date_into_a_day_type(day, want):
    assert CAL.day_type(dt.date.fromisoformat(day))[0] == want


def test_time_check_at_4pm_on_a_school_day_is_not_allowed(built):
    facts, cs = built
    out = time_check(facts, CFG, Citer(facts, cs), "2026-10-14", "16:00", None, "work", CAL)
    assert out["allowed"] is False and out["reason"] == "16:00 is after the 15:00 site-cleared time"
    assert out["day_type"] == "school_day (from the school-term calendar)" and out["sources"]
    assert time_check(facts, CFG, Citer(facts, cs), "2026-10-14", "10:00", None, "work", CAL)["allowed"] is True


def test_time_check_without_a_calendar_lists_every_window(built):
    facts, cs = built
    out = time_check(facts, CFG, Citer(facts, cs), "2026-10-14", "16:00", None, "work", None)
    assert out["allowed"] is None and "not known" in out["reason"] and out["per_day_type"]


def test_time_check_outside_the_works_dates_and_on_a_day_without_hours(built):
    facts, cs = built
    out = time_check(facts, CFG, Citer(facts, cs), "2026-11-05", "09:00", None, "work", CAL)
    assert out["allowed"] is False and "outside the works dates" in out["reason"]
    out = time_check(facts, CFG, Citer(facts, cs), "2026-10-26", "09:00", None, "work", CAL)
    assert out["allowed"] is None and "no hours for public holidays" in out["reason"]


def test_time_check_for_the_tsl_and_for_stages(built):
    facts, cs = built
    tsl = time_check(facts, CFG, Citer(facts, cs), None, "16:00", "school_day", "tsl", None)
    assert tsl["allowed"] is False and "TSL" in tsl["window"]
    stages = time_check(facts, CFG, Citer(facts, cs), None, "16:00", "school_day", "stage", None)["stages"]
    assert stages and all(s["allowed"] is not None or "hours" in s["reason"] for s in stages)


def test_uncertain_calendar_days_give_no_day_type():
    cal = Calendar((dt.date(2026, 1, 1), dt.date(2026, 12, 31)), ((dt.date(2026, 2, 9), dt.date(2026, 4, 2)),),
                   ((dt.date(2026, 1, 26), dt.date(2026, 2, 8)),))
    assert cal.day_type(dt.date(2026, 2, 3))[0] is None and cal.day_type(dt.date(2026, 2, 10))[0] == "school_day"


# --- tools --------------------------------------------------------------------------------------------------------

def test_tools_return_values_with_status_pages_conflicts_and_citations(built):
    facts, cs = built
    (sl,) = get_facts(facts, cs, CFG, "speed_limit")
    assert sl.value("rp_from").value == 0.465 and sl.source.endswith("p.18")
    card = get_facts(facts, cs, CFG, "doc_card")
    ref = next(v for it in card for v in it.values if v.field == "tmp_reference")
    assert ref.status == "conflict" and ref.conflict_ids == ["C-001"]
    st = get_status(facts, cs)
    assert st["approval"] == "partially_signed" and {c["id"] for c in st["conflicts"]} == {"C-001", "C-002", "C-003", "C-004"}
    assert any(o["status"] == "not_applicable" for o in st["signoffs"])


def test_a_diagram_is_found_by_id_number_or_title_with_its_conflicts(built):
    facts, cs = built
    assert find_sheet(facts, "Stop Go")[0] == "TMD-04" and find_sheet(facts, "TMD-03")[0] == "TMD-03"
    tid, conflicts = find_sheet(facts, "TMD4")
    assert tid == "TMD-04" and "C-002" in {c.conflict_id for c in conflicts}
    out = get_tmd(facts, cs, "TMD 04")
    assert out["found"] and out["chunk_ids"] and out["page"] == 23
    assert get_tmd(facts, cs, "TMD 09")["found"] is False


def test_pages_are_written_compactly():
    assert (pages_text([4]), pages_text([3, 10]), pages_text(list(range(1, 25))), pages_text([1, 2, 3, 7])) == \
        ("p.4", "pp.3, 10", "pp.1–24", "pp.1–3, 7")


# --- hybrid search (plan 5.7) ------------------------------------------------------------------------------------

def test_codes_match_exactly_whatever_their_spelling():
    assert code_key("TMD-04") == code_key("TMD 4") == "tmd04" and code_key("RP 0.41") == "rp0.410"
    assert tokens("delays exceeding 5mins") == ["delay", "exceed", "5", "min"]


def test_search_finds_by_code_and_respects_filters(store):
    doc = store.docs(User())[0].doc_id
    s = LocalSearch(store, CFG)
    hits = s.search(SearchRequest(doc, "RP 0.465", k=3), User())
    assert hits[0].chunk.chunk_type == "speed_limit"
    only = s.search(SearchRequest(doc, "TMD 04 Stop Go", ("sheet_overview",), k=2), User())
    assert only and {h.chunk.chunk_type for h in only if h.via == "match"} == {"sheet_overview"}
    assert s.search(SearchRequest(doc, "Stop Go", scope="generic"), User()) == []


def test_search_fuses_vectors_when_an_embedder_is_given(store):
    doc = store.docs(User())[0].doc_id
    hits = LocalSearch(store, CFG, FakeEmbeddings()).search(SearchRequest(doc, "temporary speed limit", k=4), User())
    assert hits and all(h.score > 0 for h in hits)


def _chunk(cid, ctype="narrative_table_row", parent=None, siblings=(), **md):
    from tmp_pipeline.chunks.model import Chunk
    meta = {"scope": "site_specific", "applicability": "active", "page": 1, **md}
    return Chunk(cid, ctype, f"T › {cid} — page 1", f"text {cid}", meta, parent_id=parent, siblings=list(siblings))


def test_ranking_prefers_site_answers_collapses_boilerplate_and_expands():
    rows = [_chunk(f"r{i}", parent="table") for i in range(3)]
    parent = _chunk("table", "narrative_section")
    generic = _chunk("g", "standard_rule", scope="generic")
    boiler = [_chunk(f"b{i}", "narrative_section", is_boilerplate=True, boilerplate_hash="h") for i in range(2)]
    part = _chunk("p1", "narrative_section", siblings=["p2"])
    sib = _chunk("p2", "narrative_section")
    everything = [*rows, parent, generic, *boiler, part, sib]
    fused = [(c, 1.0 / (60 + i)) for i, c in enumerate([generic, *rows, *boiler, part])]
    hits = rank_and_expand(fused, SearchRequest("d", "q", k=10), everything, CFG)
    ids = [h.chunk.chunk_id for h in hits]
    assert "g" not in ids                                       # generic only when no site answer
    assert ids.count("b0") + ids.count("b1") == 1               # identical boilerplate collapsed
    vias = {h.chunk.chunk_id: h.via for h in hits}
    assert vias["table"] == "parent" and vias["p2"] == "sibling"
    only_generic = rank_and_expand([(generic, 0.1)], SearchRequest("d", "q"), everything, CFG)
    assert [h.chunk.chunk_id for h in only_generic] == ["g"]


# --- context, safe answer and the answer check -------------------------------------------------------------------

def _evidence(store, question, types):
    doc = store.docs(User())[0]
    t = Tools(store, LocalSearch(store, CFG), CFG, User(), doc, CAL)
    reading = Reading(question, question, types, "high", D.code_details(question, R, TODAY))
    return reading, doc, run_recipes(t, reading)


def test_context_keeps_facts_and_cuts_chunks_first(store):
    reading, doc, ev = _evidence(store, "How long is the 30 km/h zone on the Stop Go layout?", ["calculation", "diagram"])
    full = assemble(reading, doc, ev, 100_000)
    assert full.text.index("FACTS") < full.text.index("CALCULATIONS") < full.text.index("CHUNKS")
    assert "0.615 km − 0.465 km = 0.150 km = 150 m" in full.text and full.chunk_ids
    tight = assemble(reading, doc, ev, 50)
    assert not tight.chunk_ids and tight.dropped and "0.150 km" in tight.text and "FACTS" in tight.text


def test_the_safe_answer_passes_the_check(store):
    for q, types in (("How long is the 30 km/h zone?", ["calculation"]), ("Is this TMP approved?", ["status"]),
                     ("Can we work at 4 pm on a school day?", ["time_check"]), ("Which diagram is TMD4?", ["diagram"]),
                     ("What's the TMP number?", ["fact"])):
        reading, doc, ev = _evidence(store, q, types)
        ctx = assemble(reading, doc, ev, R.context_budget)
        text = safe_answer(reading, ev, ctx, CFG, doc)
        assert check_answer(text, ctx) == [], (q, text)


def test_the_check_catches_numbers_pages_flags_and_generic_text(store):
    reading, doc, ev = _evidence(store, "What's the TMP number?", ["fact"])
    ctx = assemble(reading, doc, ev, R.context_budget)
    assert check_answer("The site is cleared by 1500 hrs.", ctx) == [] or "1500" not in ctx.text
    probs = check_answer("The zone is 160 m long (p.99).", ctx)
    assert "number 160 not in context" in probs and "page 99 not in context" in probs
    probs = check_answer("The TMP number is KTS 26-1581.", ctx)
    assert any(p.startswith("conflict C-001 not mentioned") for p in probs)
    assert check_answer("The TMP number is KTS 26-1581, also written KTS 26-1580 (p.1).", ctx) == []
    ctx.generic = ["SPOTTER MUST BE USED TO ENSURE THAT THE WORKER CAN CROSS THE SPACE SAFELY"]
    ctx.text += "\n" + ctx.generic[0]
    probs = check_answer("A spotter must be used to ensure that the worker can cross the space safely.", ctx)
    assert any("general standard" in p for p in probs)
    assert not any("general standard" in p for p in check_answer(
        "The general standard says a spotter must be used to ensure that the worker can cross the space safely.", ctx))


def test_numbered_list_markers_are_not_numbers_to_check(store):
    reading, doc, ev = _evidence(store, "Is this TMP approved?", ["status"])
    ctx = assemble(reading, doc, ev, R.context_budget)
    assert check_answer("Not yet:\n1. designer signed\n2. checker signed", ctx) == []
    assert "number 77 not in context" in check_answer("Not yet:\n1. designer signed\n77. nobody else", ctx)


def test_unverified_values_must_be_called_unconfirmed(store):
    reading, doc, ev = _evidence(store, "What's the TMP number?", ["fact"])
    ctx = assemble(reading, doc, ev, R.context_budget)
    v, it = next((v, it) for v, it in ctx.values if v.field == "order_reference")
    v.status = "unverified"
    assert any("unconfirmed" in p for p in check_answer(f"The order reference is {v.value}.", ctx))
    assert check_answer(f"The order reference is {v.value} (unconfirmed).", ctx) == []


# --- the whole question (plan 5.1) -------------------------------------------------------------------------------

def test_ask_without_a_model_gives_the_checked_safe_answer(store):
    res = _retriever(store).ask("How long is the 30 km/h zone?", _session(store))
    assert res.kind == "answer" and res.method == "safe (no model)" and res.problems == []
    assert "0.615 km − 0.465 km = 0.150 km = 150 m" in res.answer and 18 in res.cited_pages
    assert [c["tool"] for c in res.calls] == ["get_facts", "calculate"]
    assert set(res.latency_ms) >= {"resolve", "classify", "tools", "context", "total"}


def test_q3_answer_is_kept_when_it_passes_the_check(store):
    model = FakeClient({"Q3": lambda req: {"answer": "No. On school days the site must be cleared by 1500 hrs "
                                                     "(Planning, Shift times, p.3)."}})
    res = _retriever(store, model).ask("Can we work at 4 pm on a school day?", _session(store), User(role="site"))
    assert res.method == "Q3" and res.answer.startswith("No.") and not res.repaired
    q3 = next(c for c in model.calls if c.prompt_id == "Q3")
    assert "Style for this user: Short" in q3.system and "QUESTION: Can we work at 4 pm" in q3.user
    assert "allowed=no" in q3.user


def test_a_failed_check_gets_one_repair_then_the_safe_answer(store):
    bad = {"answer": "The zone is 160 m long (p.18)."}
    good = {"answer": "The 30 km/h zone runs from RP 0.465 to RP 0.615: 0.615 km − 0.465 km = 0.150 km = 150 m (p.18)."}
    model = FakeClient({"Q3": bad, "Q4": good})
    res = _retriever(store, model).ask("How long is the 30 km/h zone?", _session(store))
    assert res.method == "Q4" and res.repaired and res.first_problems == ["number 160 not in context"]
    q4 = next(c for c in model.calls if c.prompt_id == "Q4")
    assert "- number 160 not in context" in q4.user and "Previous answer: The zone is 160 m" in q4.user
    res = _retriever(store, FakeClient({"Q3": bad, "Q4": bad})).ask("How long is the 30 km/h zone?", _session(store))
    assert res.method == "safe (check failed)" and res.problems == [] and "150 m" in res.answer


def test_fixed_responses(store):
    r = _retriever(store)
    assert r.ask("Is KTS 99-9999 approved?", Session()).kind == "no_match"
    res = r.ask("Is it approved?", Session())
    assert res.kind == "no_tmp" and "KTS 26-1581" in res.answer
    res = r.ask("What changed in V2?", _session(store))
    assert res.kind == "answer" and "only one version" in res.answer


def test_the_session_keeps_the_tmp_and_details_across_turns(store):
    r, s = _retriever(store), Session()
    first = r.ask("What are the school day hours on KTS 26-1581?", s)
    assert first.kind == "answer" and s.tmp_ref == "KTS 26-1581" and s.details["day_type"] == "school_day"
    follow = r.ask("and the speed limit?", s)
    assert follow.kind == "answer" and follow.doc_id == first.doc_id
    assert (follow.reading.method, follow.reading.types) == ("follow-up (no model)", ["fact"])


def test_evidence_merges_without_duplicates():
    a, b = Evidence(notes=["x"]), Evidence(notes=["x", "y"])
    a.chunks = [Hit(_chunk("c1"), 1.0)]
    b.chunks = [Hit(_chunk("c1"), 0.5), Hit(_chunk("c2"), 0.4)]
    a.merge(b)
    assert [h.chunk.chunk_id for h in a.chunks] == ["c1", "c2"] and a.notes == ["x", "y"]


# --- live chat client (fake transport) ---------------------------------------------------------------------------

class FakeOpenAI:
    def __init__(self, reply=None, status=200):
        self.calls, self.reply, self.status = [], reply, status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body)))
        content = json.dumps(self.reply) if not isinstance(self.reply, str) else self.reply
        return self.status, {}, json.dumps({"model": "gpt-6-sol-2026", "usage": {"prompt_tokens": 5},
                                            "choices": [{"message": {"content": content}}]}).encode()


def test_azure_chat_sends_json_mode_messages_to_the_mapped_deployment():
    t = FakeOpenAI({"answer": "ok"})
    chat = AzureChat(ChatConfig("https://x.openai.azure.com", "secret", {"gpt-6-luna": "luna-dz"}), t)
    resp = chat.complete(prompts.q3_answer("Q?", "CTX", "Short"))
    assert resp.data == {"answer": "ok"} and resp.model == "gpt-6-sol-2026"
    method, url, headers, body = t.calls[0]
    assert "/deployments/luna-dz/chat/completions" in url and headers["api-key"] == "secret"
    assert body["reasoning_effort"] == "none"                               # answers are written without thinking
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    img = messages(ModelRequest("X7", "1", "m", "look", images=(b"png",)))
    assert img[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_model_that_rejects_temperature_0_is_asked_again_without_it():
    calls = []

    def transport(method, url, headers, body):
        body = json.loads(body)
        calls.append(body)
        if "temperature" in body:
            return 400, {}, (b'{"error": {"message": "Unsupported value: \'temperature\' does not support 0", '
                             b'"param": "temperature", "code": "unsupported_value"}}')
        return 200, {}, json.dumps({"choices": [{"message": {"content": '{"answer": "ok"}'}}]}).encode()
    chat = AzureChat(ChatConfig("https://x", "k"), transport)
    assert chat.complete(prompts.q3_answer("Q?", "CTX", "Short")).data == {"answer": "ok"}
    assert chat.complete(prompts.q3_answer("Q2?", "CTX", "Short")).data == {"answer": "ok"}
    assert ["temperature" in c for c in calls] == [True, False, False]     # remembered for the deployment


@pytest.mark.parametrize("reply,status", [("not json", 200), ({"answer": "x"}, 400), ("[1, 2]", 200)])
def test_azure_chat_failures_become_model_unavailable(reply, status):
    chat = AzureChat(ChatConfig("https://x", "k"), FakeOpenAI(reply, status))
    with pytest.raises(ModelUnavailableError):
        chat.complete(prompts.q3_answer("Q?", "CTX", "Short"))


def test_model_modes(tmp_path, monkeypatch):
    assert model_client("off") is None
    with pytest.raises(ValueError):
        model_client("replay")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(ServiceError):
        model_client("live", tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENTS", "gpt-6-sol=a, gpt-6-luna=b")
    assert ChatConfig.from_env().deployment("gpt-6-luna") == "b"
    assert type(model_client("live", tmp_path / "fx", tmp_path / "cache")).__name__ == "CachedClient"


def test_reasoning_effort_is_sent_and_dropped_for_models_that_refuse_it():
    calls = []

    def t(method, url, headers, body):
        body = json.loads(body)
        calls.append(body)
        if "reasoning_effort" in body:
            return 400, {}, (b'{"error": {"message": "Unrecognized request argument supplied: reasoning_effort", '
                             b'"param": "reasoning_effort", "code": "unsupported_parameter"}}')
        return FakeOpenAI({"answer": "ok"})(method, url, headers, json.dumps(body).encode())

    chat = AzureChat(ChatConfig("https://x", "k", reasoning_effort="low"), t)
    chat.complete(prompts.q3_answer("Q?", "CTX", "Short"))
    chat.complete(prompts.q3_answer("Q2?", "CTX", "Short"))
    assert ["reasoning_effort" in c for c in calls] == [True, False, False]


def test_keepalive_transport_reuses_one_connection_across_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from tmp_pipeline import transport as tp
    opened = []

    class Conn:
        def __init__(self, host, timeout=None, context=None):
            opened.append(host)

        def request(self, method, path, body=None, headers=None):
            self.path = path

        def getresponse(self):
            r = type("R", (), {})()
            r.status, r.headers, r.will_close, r.read = 200, {}, False, lambda: self.path.encode()
            return r

        def close(self):
            pass

    monkeypatch.setattr(tp.http.client, "HTTPSConnection", Conn)
    t = tp.KeepAliveTransport()
    assert t("POST", "https://h.example/a?x=1", {}, b"")[2] == b"/a?x=1"
    for _ in range(3):
        with ThreadPoolExecutor(1) as pool:
            pool.submit(t, "POST", "https://h.example/b", {}, b"").result()
    assert opened == ["h.example"]


def test_a_routed_model_goes_to_its_own_resource(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://main.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "main-key")
    monkeypatch.setenv("AZURE_OPENAI_ROUTES", "gpt-6-luna=LUNA")
    monkeypatch.setenv("AZURE_OPENAI_LUNA_ENDPOINT", "https://other.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_LUNA_KEY", "other-key")
    t = FakeOpenAI({"answer": "ok"})
    chat = AzureChat(ChatConfig.from_env(), t)
    chat.complete(prompts.q4_repair(["p"], "a", "Q?", "CTX"))                  # gpt-6-sol: main resource
    chat.complete(ModelRequest("Q1", "1", "gpt-6-luna", "u", "s", (), {}))     # gpt-6-luna: routed
    (_, u1, h1, _), (_, u2, h2, _) = t.calls
    assert u1.startswith("https://main.openai.azure.com/") and h1["api-key"] == "main-key"
    assert u2.startswith("https://other.openai.azure.com/openai/deployments/gpt-6-luna/") and h2["api-key"] == "other-key"
    monkeypatch.delenv("AZURE_OPENAI_LUNA_KEY")
    with pytest.raises(ServiceError):
        ChatConfig.from_env()


def test_naming_the_sheet_asked_about_is_not_using_a_conflicting_value():
    from tmp_pipeline.retrieval.check import check_answer
    from tmp_pipeline.retrieval.context import Context
    conflict = type("C", (), {"conflict_id": "C-004", "versions": [{"value": "Two-way traffic will be maintained"},
                                                                  {"value": "TMD-04 STOP GO"}]})()
    ctx = Context("TMD-04 STOP GO drawn by A Designer", 10, [23], [], [conflict], [], [], [], 0, False,
                  "On TMD-04 (Stop Go), who drew the sheet?")
    assert check_answer("TMD-04 Stop Go was drawn by A Designer (p.23).", ctx) == []
    ctx.question = "Who drew it?"
    assert any("C-004" in p for p in check_answer("TMD-04 Stop Go was drawn by A Designer (p.23).", ctx))


def test_a_conflict_version_with_other_numbers_is_not_used_by_an_answer():
    from tmp_pipeline.retrieval.check import check_answer
    from tmp_pipeline.retrieval.context import Context
    conflict = type("C", (), {"conflict_id": "C-002", "versions": [{"value": "TMD4: Parking Management"},
                                                                  {"value": "TMD 01 – Shoulder Closure"}]})()
    ctx = Context("TMD-02 Shoulder Closure, 35 m, 1 m safety zone", 10, [21], [], [conflict], [], [], [], 0, False, "On TMD-02, distances?")
    assert check_answer("TMD-02 Shoulder Closure shows 35 m (p.21).", ctx) == []
    assert check_answer("TMD-02 Shoulder Closure shows 35 m and a 1 m safety zone (p.21).", ctx) == []
    assert any("C-002" in p for p in check_answer("TMD 01 Shoulder Closure shows 35 m (p.21).", ctx))
