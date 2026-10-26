"""Plain RAG retrieval on hand-built TMPs: choosing the TMP, hybrid search, context budget, the answer check, and the
whole question through a scripted model (one Q3 call, at most one Q4 repair)."""
import copy
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from test_chunks import _chunks
from tmp_pipeline.config import load_config
from tmp_pipeline.ingestion.chunking.model import Chunk
from tmp_pipeline.ingestion.embedding.embeddings import FakeEmbeddings
from tmp_pipeline.ingestion.facts.model import FactValue
from tmp_pipeline.llm import FakeClient
from tmp_pipeline.records import Status
from tmp_pipeline.retrieval.answer import safe_answer
from tmp_pipeline.retrieval.check import check_answer
from tmp_pipeline.retrieval.context import assemble
from tmp_pipeline.retrieval.pipeline import Retriever
from tmp_pipeline.retrieval.search import (
    LocalSearch,
    SearchRequest,
    _neighbours,
    allowed,
    code_key,
    rank_and_expand,
    tokens,
)
from tmp_pipeline.retrieval.session import Session, choose_tmp, find_tmp
from tmp_pipeline.retrieval.store import DocInfo, LocalStore, User, latest

CFG = load_config()
R = CFG.retrieval
NOW = dt.datetime(2026, 10, 13, 9, 0, tzinfo=ZoneInfo("Pacific/Auckland"))     # a Tuesday in Term 4


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



# --- choosing the TMP ----------------------------------------------------------------------------------------------

def test_the_only_tmp_is_used(store):
    one = choose_tmp(store, User(), Session())
    assert (one.status, one.via, one.doc.tmp_ref) == ("one", "only TMP", "KTS 26-1581")


def test_several_tmps_and_none_chosen_ask_back(two):
    res = _retriever(two).ask("Is it approved?", Session())
    assert res.kind == "ask_back" and res.answer.startswith("I found 2 TMPs:") and "KTS 26-1700" in res.answer
    assert res.context is None and not res.tokens.get("calls")        # no search, no model call


def test_the_chosen_tmp_stays_for_the_session(two):
    s = Session()
    s.use(next(d for d in two.docs(User()) if d.tmp_ref == "KTS 26-1700"))
    res = choose_tmp(two, User(), s)
    assert (res.status, res.via, res.doc.tmp_ref) == ("one", "session", "KTS 26-1700")


def test_find_tmp_by_key_reference_alias_and_address(two):
    u = User()
    assert find_tmp(two, u, "KTS 26-1581 V1").via == "key"
    assert (find_tmp(two, u, "KTS 26-1581").via, find_tmp(two, u, "KTS 26-1581").doc.tmp_ref) == ("reference", "KTS 26-1581")
    assert find_tmp(two, u, "KTS 26-1580").doc.tmp_ref == "KTS 26-1581"          # an alias
    assert find_tmp(two, u, "65 Sunrise Ave").doc.tmp_ref == "KTS 26-1700"
    assert find_tmp(two, u, "Sunrise Ave").status == "several"
    assert find_tmp(two, u, "KTS 99-9999").status == "no_match"


def test_a_tmp_the_user_may_not_read_is_never_described(tmp_path, built):
    facts, cs = built
    _write(tmp_path, facts, cs, "a", groups=["designers"])
    store = LocalStore(tmp_path)
    assert store.docs(User()) == [] and len(store.docs(User(("designers",)))) == 1
    res = _retriever(store).ask("Is KTS 26-1581 approved?", Session(), User())
    assert (res.kind, res.answer) == ("no_access", "That TMP isn't available to you.")
    res = _retriever(store).ask("Is KTS 26-1581 approved?", Session(), User(("designers",)))
    assert res.kind == "answer" and "partially signed" in res.answer.replace("_", " ")


def test_latest_version_wins():
    docs = [DocInfo("a", "KTS 1", version=1), DocInfo("b", "KTS 1", version=2), DocInfo("c", "KTS 2")]
    assert {d.doc_id for d in latest(docs)} == {"b", "c"}


# --- hybrid search -------------------------------------------------------------------------------------------------

def test_codes_match_exactly_whatever_their_spelling():
    assert code_key("TMD-04") == code_key("TMD 4") == "tmd04" and code_key("RP 0.41") == "rp0.410"
    assert tokens("delays exceeding 5mins") == ["delay", "exceed", "5", "min"]


def test_search_finds_by_code_and_skips_struck_chunks(store):
    doc = store.docs(User())[0].doc_id
    s = LocalSearch(store, CFG)
    hits = s.search(SearchRequest(doc, "RP 0.465", k=3), User())
    assert hits[0].chunk.chunk_type == "speed_limit"
    assert all(h.chunk.metadata.get("applicability", "active") == "active" for h in hits)


def test_search_fuses_vectors_when_an_embedder_is_given(store):
    doc = store.docs(User())[0].doc_id
    hits = LocalSearch(store, CFG, FakeEmbeddings()).search(SearchRequest(doc, "temporary speed limit", k=4), User())
    assert hits and all(h.score > 0 for h in hits)


def _chunk(cid, ctype="narrative_table_row", parent=None, siblings=(), **md):
    meta = {"scope": "site_specific", "applicability": "active", "page": 1, **md}
    return Chunk(cid, ctype, f"T › {cid} — page 1", f"text {cid}", meta, parent_id=parent, siblings=list(siblings))


def test_ranking_caps_general_standard_collapses_boilerplate_and_expands():
    rows = [_chunk(f"r{i}", parent="table") for i in range(3)]
    parent = _chunk("table", "narrative_section")
    generic = [_chunk(f"g{i}", "standard_rule", scope="generic") for i in range(3)]
    boiler = [_chunk(f"b{i}", "narrative_section", is_boilerplate=True, boilerplate_hash="h") for i in range(2)]
    part = _chunk("p1", "narrative_section", siblings=["p2"])
    sib = _chunk("p2", "narrative_section")
    everything = [*rows, parent, *generic, *boiler, part, sib]
    fused = [(c, 1.0 / (60 + i)) for i, c in enumerate([*generic, *rows, *boiler, part])]
    hits = rank_and_expand(fused, SearchRequest("d", "q", k=10), everything, CFG)
    ids = [h.chunk.chunk_id for h in hits]
    assert [i for i in ids if i.startswith("g")] == ["g0", "g1"]   # at most two general-standard chunks
    assert ids.count("b0") + ids.count("b1") == 1               # identical boilerplate collapsed
    vias = {h.chunk.chunk_id: h.via for h in hits}
    assert vias["table"] == "parent" and vias["p2"] == "sibling"
    only_generic = rank_and_expand([(generic[0], 0.1)], SearchRequest("d", "q"), everything, CFG)
    assert [h.chunk.chunk_id for h in only_generic] == ["g0"]


def test_the_other_side_of_a_conflict_is_the_cheapest_chunk_that_shows_it():
    best = _chunk("best", "register_row", conflict_ids=["C-009"])
    big, small = _chunk("big", "narrative_section", conflict_ids=["C-009"]), _chunk("small", "stage", conflict_ids=["C-009"])
    big.tokens, small.tokens = 480, 90
    hits = rank_and_expand([(best, 0.1)], SearchRequest("d", "q", k=3), [best, big, small], CFG)
    assert [(h.chunk.chunk_id, h.via) for h in hits] == [("best", "match"), ("small", "conflict")]


def test_a_numbered_part_brings_only_the_adjacent_parts():
    parts = [_chunk(f"s:part-{i}", "narrative_section", siblings=[f"s:part-{j}" for j in range(1, 5) if j != i])
             for i in range(1, 5)]
    hits = rank_and_expand([(parts[1], 0.1)], SearchRequest("d", "q", k=5), parts, CFG)
    assert {h.chunk.chunk_id for h in hits} == {"s:part-1", "s:part-2", "s:part-3"}


def _with(**retrieval):
    import dataclasses
    return dataclasses.replace(CFG, retrieval=dataclasses.replace(CFG.retrieval, **retrieval))


def test_weak_keyword_matches_are_dropped_but_the_best_is_kept(store):
    doc = store.docs(User())[0].doc_id
    q = "temporary speed limit hours on Sunrise Avenue"
    everything = LocalSearch(store, _with(min_score_ratio=0.0)).search(SearchRequest(doc, q, k=20), User())
    cut = LocalSearch(store, _with(min_score_ratio=0.5)).search(SearchRequest(doc, q, k=20), User())
    assert 0 < len(cut) < len(everything)
    assert cut[0].chunk.chunk_id == everything[0].chunk.chunk_id


def test_a_search_adds_at_most_the_configured_number_of_linked_sheet_overviews():
    ov = [_chunk(f"ov{i}", "sheet_overview", tmd_ids=[f"TMD-0{i}"]) for i in (1, 2, 3)]
    hit = _chunk("h", "register_row", linked_sheets=["TMD-01", "TMD-02"], tmd_ids=["TMD-01", "TMD-02"])
    for n in (0, 1, 2):
        hits = rank_and_expand([(hit, 0.1)], SearchRequest("d", "q", k=5), [hit, *ov], _with(max_linked_sheets=n))
        assert len([h for h in hits if h.via == "linked_sheet"]) == n


def test_only_active_chunks_are_searched_and_neighbours_are_the_adjacent_parts():
    c = Chunk("d:sheet_layout:tmd-04:part-3", "sheet_layout", "", "", {"tmd_ids": ["TMD-04"]},
              siblings=[f"d:sheet_layout:tmd-04:part-{n}" for n in (1, 2, 4, 5)])
    assert _neighbours(c) == ["d:sheet_layout:tmd-04:part-2", "d:sheet_layout:tmd-04:part-4"]
    struck = _chunk("s", applicability="not_applicable")
    assert allowed(c, SearchRequest("d", "q")) and not allowed(struck, SearchRequest("d", "q"))
    assert allowed(struck, SearchRequest("d", "q", include_not_applicable=True))


# --- context, safe answer and the answer check ---------------------------------------------------------------------

def _context(store, question, budget=None):
    doc = store.docs(User())[0]
    hits = LocalSearch(store, CFG).search(SearchRequest(doc.doc_id, question, R.search_k), User())
    return assemble(question, doc, hits, budget or R.context_budget)


def test_context_is_the_tmp_line_and_the_chunks_and_cuts_chunks_from_the_bottom(store):
    q = "What is the temporary speed limit on Sunrise Avenue?"
    full = _context(store, q, 100_000)
    assert full.text.startswith("TMP: KTS 26-1581") and "\nCHUNKS\n[1] " in full.text and full.chunk_ids
    assert 18 in full.pages and "FACTS" not in full.text
    tight = _context(store, q, 50)
    assert not tight.chunk_ids and tight.dropped and tight.text.startswith("TMP: KTS 26-1581")


def test_the_safe_answer_passes_the_check(store):
    for q in ("What is the temporary speed limit?", "Is this TMP approved?", "Which diagram is TMD4?",
              "What's the TMP number?", "Who is the contractor?"):
        ctx = _context(store, q)
        text = safe_answer(ctx, CFG)
        assert check_answer(text, ctx) == [], (q, text)


def test_the_check_catches_numbers_pages_and_generic_text(store):
    ctx = _context(store, "What's the TMP number?")
    probs = check_answer("The zone is 160 m long (p.99).", ctx)
    assert "number 160 not in context" in probs and "page 99 not in context" in probs
    ctx.hits = [_hit("SPOTTER MUST BE USED TO ENSURE THAT THE WORKER CAN CROSS THE SPACE SAFELY", scope="generic")]
    ctx.text += "\n" + ctx.hits[0].chunk.content
    probs = check_answer("A spotter must be used to ensure that the worker can cross the space safely.", ctx)
    assert any("general standard" in p for p in probs)
    assert not any("general standard" in p for p in check_answer(
        "The general standard says a spotter must be used to ensure that the worker can cross the space safely.", ctx))


def _hit(content, **md):
    from tmp_pipeline.retrieval.search import Hit
    c = _chunk("x", "standard_rule", **md)
    c.content = content
    return Hit(c, 1.0)


def test_numbered_list_markers_are_not_numbers_to_check(store):
    ctx = _context(store, "Is this TMP approved?")
    assert check_answer("Not yet:\n1. designer signed\n2. checker signed", ctx) == []
    assert "number 77 not in context" in check_answer("Not yet:\n1. designer signed\n77. nobody else", ctx)


# --- the whole question --------------------------------------------------------------------------------------------

def test_ask_without_a_model_gives_the_checked_safe_answer(store):
    res = _retriever(store).ask("What is the temporary speed limit on Sunrise Avenue?", _session(store))
    assert res.kind == "answer" and res.method == "safe (no model)" and res.problems == []
    assert "30 km/h" in res.answer and 18 in res.cited_pages
    assert set(res.latency_ms) >= {"retrieve", "context", "answer", "check", "total"}


def test_one_model_call_writes_the_answer_when_it_passes_the_check(store):
    model = FakeClient({"Q3": lambda req: {"answer": "The temporary speed limit is 30 km/h (Planning, Temporary "
                                                     "speed limits, page 18)."}})
    res = _retriever(store, model).ask("What is the speed limit?", _session(store), User(role="site"))
    assert res.method == "Q3" and res.answer.startswith("The temporary") and not res.repaired
    assert [c.prompt_id for c in model.calls] == ["Q3"]
    q3 = model.calls[0]
    assert "Style for this user: Short" in q3.system and "QUESTION: What is the speed limit?" in q3.user
    assert "Today (NZ time): 2026-10-13 Tuesday 09:00" in q3.user and "TMP: KTS 26-1581" in q3.user


def test_a_failed_check_gets_one_repair_then_the_safe_answer(store):
    bad = {"answer": "The zone is 160 m long (p.18)."}
    good = {"answer": "The temporary speed limit is 30 km/h (p.18)."}
    model = FakeClient({"Q3": bad, "Q4": good})
    res = _retriever(store, model).ask("What is the speed limit?", _session(store))
    assert res.method == "Q4" and res.repaired and res.first_problems == ["number 160 not in context"]
    q4 = next(c for c in model.calls if c.prompt_id == "Q4")
    assert "- number 160 not in context" in q4.user and "Previous answer: The zone is 160 m" in q4.user
    res = _retriever(store, FakeClient({"Q3": bad, "Q4": bad})).ask("What is the speed limit?", _session(store))
    assert res.method == "safe (check failed)" and res.problems == [] and "30 km/h" in res.answer


def test_a_follow_up_is_searched_with_the_last_question_and_sees_the_turns(store):
    seen = []

    class Spy(LocalSearch):
        def search(self, req, user):
            seen.append(req.query)
            return super().search(req, user)
    model = FakeClient({"Q3": {"answer": "The temporary speed limit is 30 km/h (p.18)."}})
    r = Retriever(store, Spy(store, CFG), CFG, model, lambda: NOW)
    s = _session(store)
    r.ask("What is the speed limit?", s)
    r.ask("and the hours?", s)
    assert seen == ["What is the speed limit?", "What is the speed limit? and the hours?"]
    assert "Q: What is the speed limit?" in model.calls[1].user
    s2 = Session()
    s2.use(store.docs(User())[0])
    assert s2.turns == []


def test_switching_tmp_clears_the_conversation(two):
    s = Session()
    a, b = two.docs(User())
    s.use(a)
    s.remember("q", "a")
    s.use(a)
    assert s.turns
    s.use(b)
    assert s.turns == []


def test_the_question_log_records_the_flow_without_breaking_the_logger(store, tmp_path):
    import json

    from tmp_pipeline import logging_ as lg
    lg.start_run(tmp_path / "ask.log")
    try:
        _retriever(store).ask("What is the speed limit?", _session(store))
    finally:
        lg.end_run()
    events = [json.loads(x) for x in (tmp_path / "ask.log").read_text().splitlines()]
    answered = next(e for e in events if e["event"] == "answered")
    assert answered["kind"] == "answer" and answered["level"] == "info" and answered["chunks"]
