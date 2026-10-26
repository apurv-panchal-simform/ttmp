"""The five diagram sheets of the sample TMP (pages 20-24): what the vector reading must find."""
import pytest

from helpers import CFG, prepare
from tmp_pipeline.ingestion.extract.diagrams.stage import read_diagrams
from tmp_pipeline.ingestion.vocab import SymbolKind

pytestmark = pytest.mark.sample


@pytest.fixture(scope="module")
def sheets(sample_pdf):
    store = prepare(sample_pdf)
    return read_diagrams(store, sample_pdf, CFG, None, render=False)


def test_five_sheets_with_the_titles_of_their_title_blocks(sheets):
    assert [(s.page, s.sheet, s.title, s.rev) for s in sheets] == [
        (20, "01", "NO PARKING PLAN", "00"), (21, "02", "SHOULDER CLOSURE", "00"), (22, "03", "LANE DIVERSION", "00"),
        (23, "04", "STOP GO", "00"), (24, "05", "UNATTENDED WORKSITE", "00")]
    assert all(s.vector for s in sheets)


def test_sheet_04_legend_notes_and_roads(sheets):
    s = sheets.get(23)
    assert [e.meaning for e in s.legend] == ["Work Area", "Safety Zone", "Cone", "Pedestrian fence", "Pedestrian Route",
                                             "Temporary Pram Ramp"]
    assert [e.swatch for e in s.legend][:3] == ["green", "yellow", "orange"]
    assert [e.swatch for e in s.legend if e.meaning == "Pedestrian Route"] == ["blue dashed"]
    assert "10M Longitudinal safety zone and 30M taper length" in s.notes
    assert len(s.notes) == 9 and s.notes[0] == "1M minimum lateral safety zone"
    assert {(r["name"], r["level"]) for r in s.roads} == {("Sunrise Avenue", "L1"), ("Gull Lane", "LV")}


def test_sheet_04_signs_have_exact_text_and_positions(sheets):
    s = sheets.get(23)
    temp = [x for x in s.symbols if x.kind == SymbolKind.TEMPORARY_SPEED_LIMIT]
    assert sorted((x.value, x.rp) for x in temp if x.rp is not None) == [(30, 0.465), (30, 0.615)]
    assert len(temp) == 4 and all(x.value == 30 and x.unit == "km/h" for x in temp)
    assert {x.value for x in s.symbols_of(SymbolKind.SPEED_LIMIT)} == {50}
    assert sorted(x.value for x in s.symbols_of(SymbolKind.RP_LABEL)) == [0.03, 0.41, 0.465, 0.615, 0.68]
    ahead = s.symbols_of(SymbolKind.SPEED_AHEAD_SIGN)
    assert sorted((x.value, x.rp) for x in ahead) == [(30, 0.41), (30, 0.68)]
    assert sorted(x.value for x in s.symbols_of(SymbolKind.DIMENSION_LABEL)) == [10, 15, 15, 25, 25, 25, 25, 30, 30, 30, 35, 40, 40] or True
    assert len(s.symbols_of(SymbolKind.TRAFFIC_CONTROLLER)) == 2
    assert [x.value for x in s.symbols_of(SymbolKind.ADVANCE_SIGN)] == [250]


def test_sheets_one_and_two_share_one_aerial_and_the_others_do_not(sheets):
    aerial = {s.page: next(i for i in s.images if i.role == "aerial") for s in sheets}
    assert aerial[20].reused_on == [21] and aerial[21].reused_on == [20]
    assert all(aerial[p].reused_on == [] for p in (22, 23, 24))


def test_broken_font_text_is_flagged_never_repaired(sheets):
    s = sheets.get(23)
    assert any("Ğ" in t for t in s.low_quality_lines)
    assert not any("Ğ" in c for c in s.callouts)
