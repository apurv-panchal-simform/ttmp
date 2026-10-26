"""End-to-end Phase 1 checks on the real sample TMP (plan section 8.3 exit: sample fully routed, coverage 100%)."""
import json
from collections import Counter

import pytest

from tmp_pipeline.ingestion.run import main, run


@pytest.fixture(scope="module")
def result(tmp_path_factory, request):
    sample = request.getfixturevalue("sample_pdf")
    return run(sample, tmp_path_factory.mktemp("out"), render=False)


def test_sample_zones_match_the_plan(result):
    z = {p: i.zone for p, i in result.store.pages.items()}
    assert [z[p] for p in (1, 2, 3, 4)] == ["forms"] * 4
    assert [z[p] for p in range(6, 10)] == ["risk"] * 4
    assert [z[p] for p in range(10, 17)] == ["narrative"] * 7
    assert z[17] == "blank"
    assert z[18] == z[19] == "forms"
    assert [z[p] for p in range(20, 25)] == ["diagram"] * 5
    assert z[25] == "generic_standard"
    assert len(z) == 25


def test_page_5_has_two_zones(result):
    zones = Counter(e.zone for e in result.store.on_page(5) if e.type in ("text_block", "checkbox", "table"))
    assert zones["forms"] > 0 and zones["risk"] > 0
    (matrix,) = [e for e in result.store.of_type("region", page=5)]
    assert matrix.zone == "risk" and matrix.bbox_pt[3] < 300        # lower part of the page


def test_coverage_is_complete_with_one_log_line_per_element(result):
    cov = result.router.coverage(result.store)
    assert cov["coverage"] == 1.0 and not cov["missing"] and not cov["duplicated"]
    lines = (result.out_dir / "router.log").read_text().splitlines()
    assert len(lines) == len(result.store.elements)
    assert all(json.loads(l)["outcome"] for l in lines)


def test_diagram_pages_are_flagged_low_text_quality(result):
    flagged = {e.page for e in result.store.elements.values() if "low_text_quality" in e.flags}
    assert {20, 21, 22, 23, 24} <= flagged


def test_header_footer_values_are_metadata_not_content(result):
    s = result.store
    assert s.metadata["tmp_reference"] == "KTS 26-1581"
    assert s.pages[5].printed_page == 5 and s.pages[18].printed_page == 18
    assert s.metadata["form_names"]["10"].lower().startswith("2. tmp job pack")
    assert not any("TMP Reference" in e.representations.get("text", "") for e in s.of_type("text_block"))
    assert s.of_type("page_furniture")


def test_thirty_checkboxes_on_the_expected_pages(result):
    cbs = result.store.of_type("checkbox")
    assert {c.page for c in cbs} == {4, 5, 9, 11, 19}
    assert len(cbs) == 30


def test_outputs_written_and_second_run_is_skipped(result, sample_pdf):
    for name in ("elements.json", "router.log", "run.log", "report.md"):
        assert (result.out_dir / name).exists()
    out_root = result.out_dir.parent
    again = run(sample_pdf, out_root, render=False)
    assert again.skipped is True
    forced = run(sample_pdf, out_root, render=False, force=True)
    assert forced.skipped is False


def test_cli_exits_zero(sample_pdf, tmp_path, capsys):
    assert main([str(sample_pdf), "--out", str(tmp_path), "--skip-render"]) == 0
    assert "coverage 100%" in capsys.readouterr().out


def test_run_log_lines_all_carry_run_and_doc_id(result):
    for line in (result.out_dir / "run.log").read_text().splitlines():
        rec = json.loads(line)
        assert rec["run_id"] and rec["doc_id"].startswith("sha256:")


def test_every_element_has_a_pixel_box(result):
    assert all(e.bbox_px and len(e.bbox_px) == 4 for e in result.store.elements.values())


def test_phase_4_facts_on_the_sample(result):
    """Plan 8.3 phase 4 exit: all four conflicts and partially_signed detected."""
    f = result.facts
    assert [(c.conflict_id, c.rule) for c in f.conflicts] == [("C-001", 1), ("C-002", 2), ("C-003", 3), ("C-004", 4)]
    assert f.approval.status == "partially_signed"
    assert f.approval.unsigned == ["activity_manager", "rca_primary", "rca_authorisation_form3"]
    assert {s.role: s.status for s in f.signoffs}["rca_other"] == "not_applicable"
    assert [s.tmd_id for s in f.tmd_sheets] == ["TMD-01", "TMD-02", "TMD-03", "TMD-04", "TMD-05"]
    assert f.coverage["complete"]
    out = result.out_dir
    assert json.loads((out / "facts.json").read_text())["approval"]["status"] == "partially_signed"
    assert json.loads((out / "review.json").read_text())["items"]
    assert "## Facts and checks" in (out / "report.md").read_text()


def test_phase_5_chunks_on_the_sample(result):
    """Plan 8.3 phase 5 exit: lineage 100%; sizes within the hard maximum; every applicable chunk type present."""
    cs, lineage = result.chunks, result.lineage
    assert lineage["complete"], lineage["gaps"][:5]
    assert all(c.tokens <= 600 for c in cs.chunks)
    types = {c.chunk_type for c in cs.chunks}
    assert types >= {"doc_card", "stakeholders", "activity", "road", "schedule", "speed_limit", "stage", "form_fields",
                     "yes_no_questions", "vulnerable_user", "risk_matrix", "register_row", "narrative_section",
                     "narrative_table_row", "procedure", "sheet_overview", "sheet_sign_schedule", "sheet_notes",
                     "figure_caption", "signoff", "standard_rule", "register_summary", "form_answers_summary"}
    assert len({c.chunk_id for c in cs.chunks}) == len(cs.chunks)
    stop_go = next(c for c in cs.of("register_row") if c.chunk_id.endswith(":stop-go"))
    assert stop_go.header.startswith("KTS 26-1581 \u203a 1. TMP Planning Form \u203a Risk register \u203a Stop Go \u2014 page 6")
    assert "Affected diagram: TMD-04 Stop Go" in stop_go.content
    by_rating = next(c for c in cs.of("register_summary") if c.chunk_id.endswith(":by-rating"))
    very_high = next(ln for ln in by_rating.content.splitlines() if ln.startswith("Initial risk Very High ("))
    assert very_high.startswith("Initial risk Very High (11): ")
    assert len(very_high.split(": ", 1)[1].split(", ")) == 11
    assert "Initial risk Very High → residual Low (1): Excavation" in by_rating.content
    answers = next(iter(cs.of("form_answers_summary")))
    assert "Answered yes (3): " in answers.content and "Vulnerable road users not ticked (1): Aged care/mobility" in answers.content
    assert (result.out_dir / "chunks.jsonl").exists() and json.loads((result.out_dir / "lineage.json").read_text())["complete"]

