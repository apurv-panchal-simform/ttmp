"""Shared fixtures and helpers: small synthetic PDFs built with reportlab."""
import os
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from tmp_pipeline import logging_ as lg

SAMPLE = Path(__file__).parent / "data" / "sample.pdf"

@pytest.fixture(scope="session")
def sample_pdf() -> Path:
    if not SAMPLE.exists():
        # CI sets TMP_REQUIRE_SAMPLE=1 so a missing sample fails instead of passing vacuously
        if os.environ.get("TMP_REQUIRE_SAMPLE") == "1":
            pytest.fail(f"sample PDF required but missing: {SAMPLE}")
        pytest.skip("sample PDF not available")
    return SAMPLE

@pytest.fixture
def make_pdf(tmp_path):
    """make_pdf(name, pages) where pages is a list of callables taking (canvas, width, height)."""
    def _make(name: str, pages, size=landscape(A4)) -> Path:
        path = tmp_path / name
        c = canvas.Canvas(str(path), pagesize=size)
        w, h = size
        for draw in pages:
            draw(c, w, h)
            c.showPage()
        c.save()
        return path
    return _make

@pytest.fixture(autouse=True)
def _reset_logging():
    """A test that fails before end_run() must not leak an open log sink into the next one."""
    yield
    lg.end_run()
