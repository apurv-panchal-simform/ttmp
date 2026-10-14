"""The question test set (plan 7.2) on the sample TMP, offline: every deterministic measure must be 100% (the rules
read each question, the context holds the facts and pages the answer needs, no answer carries a number the context
lacks, the fixed responses fire). Answer measures need a model and are reported by `tmp-pipeline-qa --model live`."""
import datetime as dt
import json
from pathlib import Path

import pytest

from tmp_pipeline.config import load_config
from tmp_pipeline.gold.questions import DETERMINISTIC, render, run_questions
from tmp_pipeline.retrieval.hybrid import LocalSearch
from tmp_pipeline.retrieval.pipeline import Retriever
from tmp_pipeline.retrieval.store import LocalStore
from tmp_pipeline.run import run

QUESTIONS = Path(__file__).parent / "data" / "gold" / "sample.questions.json"
pytestmark = pytest.mark.sample


@pytest.fixture(scope="module")
def score(sample_pdf, tmp_path_factory):
    res = run(sample_pdf, tmp_path_factory.mktemp("out"), render=False)
    gold = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    cfg = load_config()
    store = LocalStore(res.out_dir)
    today = dt.datetime.fromisoformat(gold["today"])
    return run_questions(Retriever(store, LocalSearch(store, cfg), cfg, None, lambda: today), gold)


def test_every_deterministic_measure_is_met_on_the_sample(score):
    assert score.deterministic_ok(), render(score)
    for name in DETERMINISTIC:
        ok, n = score.measure(name)
        assert n and ok == n, (name, render(score))


def test_the_worked_examples_answer_offline(score):
    by_id = {r.qid: r for r in score.results}
    assert "0.615 km − 0.465 km = 0.150 km = 150 m" in by_id["q17"].answer and "p.18" in by_id["q17"].answer
    assert by_id["q13"].answer.startswith("No.") and "15:00" in by_id["q13"].answer
    assert "partially signed" in by_id["q35"].answer
    assert "KTS 26-1580" in by_id["q02"].answer and "KTS 26-1581" in by_id["q02"].answer
    assert by_id["q44"].kind == "no_match" and by_id["q46"].kind == "no_tmp"
    assert all(not r.problems for r in score.results if r.kind == "answer")
