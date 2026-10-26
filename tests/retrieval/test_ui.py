"""The Streamlit pages render a real (synthetic) run folder without exceptions, and Ask answers with model off."""
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from helpers import header_footer  # noqa: E402
from tmp_pipeline.ingestion.run import run  # noqa: E402

VIEWS = ["overview", "elements", "records", "diagrams", "facts", "chunks", "ask", "qa", "run"]


def _page(c, w, h):
    header_footer(c, w, h)
    c.setFont("Helvetica", 10)
    c.drawString(72, 450, "Pedestrians will be escorted past the worksite at all times")


@pytest.fixture(scope="module")
def doc(tmp_path_factory):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    root = tmp_path_factory.mktemp("ui")
    pdf = root / "ui.pdf"
    c = canvas.Canvas(str(pdf), pagesize=landscape(A4))
    for _ in range(2):
        _page(c, *landscape(A4))
        c.showPage()
    c.save()
    res = run(pdf, root / "out", render=True)
    return root / "out", res.out_dir


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The Ask page has no settings: it is pointed at the local chunks and the model is off through the environment."""
    monkeypatch.setenv("TMP_UI_MODEL", "off")
    monkeypatch.setenv("TMP_UI_BACKEND", "local")


def _app(view: str, root, doc_dir) -> AppTest:
    at = AppTest.from_string(f"from tmp_pipeline.retrieval.ui.views import {view}\n{view}.render()\n", default_timeout=60)
    at.session_state["root"] = str(root)
    at.session_state["doc"] = str(doc_dir)
    return at


@pytest.mark.parametrize("view", VIEWS)
def test_every_page_renders_a_run_folder(view, doc):
    at = _app(view, *doc).run()
    assert not at.exception, at.exception


def test_elements_page_draws_chunks(doc):
    at = _app("elements", *doc).run()
    at.selectbox(key="el_colour").select("chunk").run()
    assert not at.exception, at.exception


def test_list_docs_finds_the_run(doc):
    from tmp_pipeline.retrieval.ui.data import list_docs
    docs = list_docs(doc[0])
    assert [d.path for d in docs] == [doc[1]] and docs[0].status == "done"


def test_ask_answers_with_model_off(doc):
    at = _app("ask", *doc).run()
    at.chat_input[0].set_value("What is the TMP reference?").run()
    assert not at.exception, at.exception
    assert len(at.session_state["ask_history"]) == 1
