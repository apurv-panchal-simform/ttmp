"""Shared fixtures: small synthetic PDFs built with reportlab."""
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

SAMPLE = Path(__file__).parent / "data" / "sample.pdf"


@pytest.fixture(scope="session")
def sample_pdf() -> Path:
    if not SAMPLE.exists():
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
