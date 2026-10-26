"""Run lifecycle on synthetic PDFs: one log per document, failure evidence, typed input errors, CLI exit codes."""
import json

import pytest

from helpers import header_footer
from tmp_pipeline.errors import CorruptPdfError, EmptyPdfError, EncryptedPdfError
from tmp_pipeline.ingestion import run as run_mod
from tmp_pipeline.ingestion.contracts import BaseHandler
from tmp_pipeline.ingestion.extract.handlers.stubs import stub_registry
from tmp_pipeline.ingestion.extract.intake import doc_dir, intake
from tmp_pipeline.ingestion.run import main, run
from tmp_pipeline.ingestion.vocab import Route


def _page(c, w, h):
    header_footer(c, w, h)
    c.setFont("Helvetica", 10)
    c.drawString(72, 450, "Pedestrians will be escorted past the worksite at all times")


def _pdf(make_pdf, name="r.pdf"):
    return make_pdf(name, [_page, _page, _page])


def test_one_run_log_per_document_with_stages_and_no_global_runs_log(make_pdf, tmp_path):
    res = run(_pdf(make_pdf), tmp_path, render=False)
    log = res.out_dir / "run.log"
    events = [(r["stage"], r["event"]) for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())]
    assert ("intake", "stage_started") in events and ("router", "stage_finished") in events
    assert events[-1] == ("run", "done")
    assert not (tmp_path / "_runs.log").exists()
    assert {r["doc_id"] for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())} == {res.intake.doc_id}
    assert (res.out_dir / "records.json").exists() and not (res.out_dir / "FAILED").exists()


def test_failure_leaves_a_marker_a_partial_store_and_the_error_in_the_log(make_pdf, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("zones exploded")
    monkeypatch.setattr(run_mod, "assign_zones", boom)
    pdf = _pdf(make_pdf)
    with pytest.raises(RuntimeError, match="zones exploded"):
        run(pdf, tmp_path, render=False)
    out = next(p for p in tmp_path.iterdir() if p.is_dir())
    marker = json.loads((out / "FAILED").read_text(encoding="utf-8"))
    assert marker["stage"] == "zones" and "zones exploded" in marker["error"] and "Traceback" in marker["trace"]
    assert (out / "elements.partial.json").exists() and not (out / "elements.json").exists()
    last = [json.loads(x) for x in (out / "run.log").read_text(encoding="utf-8").splitlines()][-1]
    assert (last["level"], last["event"]) == ("error", "stage_failed")
    assert not (out / ".done").exists()
    monkeypatch.undo()                                       # a later good run clears the marker and completes
    again = run(pdf, tmp_path, render=False)
    assert not (again.out_dir / "FAILED").exists() and (again.out_dir / ".done").exists()


def test_a_failing_handler_does_not_fail_the_run_and_is_reported(make_pdf, tmp_path):
    class Boom(BaseHandler):
        route, version = Route.TEXT, "t"

        def handle(self, ctx, element):
            raise RuntimeError("handler broke")
    reg = stub_registry()
    reg.replace(Boom())
    res = run(_pdf(make_pdf), tmp_path, render=False, registry=reg)
    assert res.router.failures and res.router.coverage(res.store)["coverage"] == 1.0
    assert "handler broke" in (res.out_dir / "report.md").read_text(encoding="utf-8")


def test_typed_errors_for_corrupt_empty_and_encrypted_pdfs(make_pdf, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(CorruptPdfError):
        intake(bad, tmp_path / "o")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(CorruptPdfError):
        intake(empty, tmp_path / "o")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas
    enc = tmp_path / "enc.pdf"
    c = canvas.Canvas(str(enc), pagesize=A4, encrypt=StandardEncryption("secret", canPrint=0))
    c.drawString(72, 700, "hidden")
    c.showPage()
    c.save()
    with pytest.raises(EncryptedPdfError):
        intake(enc, tmp_path / "o")
    assert issubclass(EmptyPdfError, Exception)


def test_cli_exit_codes_and_messages(make_pdf, tmp_path, capsys):
    pdf = _pdf(make_pdf)
    assert main([str(pdf), "--out", str(tmp_path / "o"), "--skip-render"]) == 0
    assert main([str(pdf), "--out", str(tmp_path / "o"), "--skip-render"]) == 0           # skipped
    assert "already processed" in capsys.readouterr().out
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert main([str(bad), "--out", str(tmp_path / "o")]) == 2
    assert "not a readable PDF" in capsys.readouterr().err
    t = tmp_path / "t.toml"
    t.write_text("[page]\ntypo = 1\n")
    assert main([str(pdf), "--out", str(tmp_path / "o2"), "--template", str(t)]) == 2
    assert "unknown key" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.pdf"), "--out", str(tmp_path / "o3")]) == 2


def test_doc_dir_is_named_from_the_content_hash(make_pdf, tmp_path):
    res = run(_pdf(make_pdf), tmp_path, render=False)
    assert res.out_dir == doc_dir(tmp_path, res.intake.doc_id)
