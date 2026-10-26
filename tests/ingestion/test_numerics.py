import pytest

from tmp_pipeline.config import load_config
from tmp_pipeline.ingestion.numerics import extract, parse_value


def one(text, kind):
    hits = [n for n in extract(text) if n.kind == kind]
    assert len(hits) == 1, f"{kind} in {text!r}: {hits}"
    return hits[0]


@pytest.mark.parametrize("text,kind,value,unit", [
    ("RP 0.410", "rp", 0.41, "km"),
    ("RP 0.680", "rp", 0.68, "km"),
    ("posted speed 50 km/h here", "speed", 50, "km/h"),
    ("limit of 30km/h", "speed", 30, "km/h"),
    ("exceeding 250m in length", "distance", 250, "m"),
    ("Road width: 9.9 m", "distance", 9.9, "m"),
    ("within 50m of the school", "distance", 50, "m"),
    ("2 km of road", "distance", 2, "km"),
    ("crane of 1.5 t", "weight", 1.5, "t"),
    ("load 800 kg", "weight", 800, "kg"),
    ("First Sign In: 0900 hrs", "time", "09:00", None),
    ("Pick up 1430 hrs", "time", "14:30", None),
    ("opens at 7am", "time", "07:00", None),
    ("closes 5pm", "time", "17:00", None),
    ("at 9:30 pm", "time", "21:30", None),
    ("28 September 2026", "date", "2026-09-28", None),
    ("ends 1 October 2026.", "date", "2026-10-01", None),
    ("Date 16/09/2026", "date", "2026-09-16", None),
    ("3%", "percent", 3.0, "%"),
    ("5.409%", "percent", 5.409, "%"),
    ("An AADT of 1158 vph typically", "flow", 1158, "vph"),
    ("4 shifts", "duration", 4, "shift"),
    ("every 2 hours", "duration", 2, "h"),
    ("delays exceeding 5mins", "duration", 5, "min"),
    ("TMD 04", "tmd", "TMD-04", None),
    ("TMD4: Parking Management", "tmd", "TMD-04", None),
    ("TMD Sheet 04 Stop Go", "tmd", "TMD-04", None),
    ("TMD 02 / TMD 03 / TMD 04", "tmd", None, None),            # three hits: checked separately below
    ("KTS 26-1581", "tmp_reference", "KTS 26-1581", None),
    ("KTS26-1581", "tmp_reference", "KTS 26-1581", None),
    ("call 0800 USPILL now", "phone", "0800 USPILL", None),
    ("0800 630 7200", "phone", "0800 630 7200", None),
    ("ring 09 123 4567", "phone", "09 123 4567", None),
])
def test_typed_extraction(text, kind, value, unit):
    hits = [n for n in extract(text) if n.kind == kind]
    if value is None:
        assert len(hits) == 3
        return
    n = one(text, kind)
    assert (n.value, n.unit) == (value, unit)
    assert text[n.start:n.end] == n.raw                       # offsets point at the raw text


def test_three_tmd_references_in_one_cell_are_all_found_in_order():
    vals = [n.value for n in extract("TMD 02 / TMD 03 / TMD 04") if n.kind == "tmd"]
    assert vals == ["TMD-02", "TMD-03", "TMD-04"]


def test_peak_hours_phrase_gives_both_times():
    assert [(n.value) for n in extract("between 7am to 8am and 5pm to 7pm") if n.kind == "time"] == [
        "07:00", "08:00", "17:00", "19:00"]


def test_overlaps_go_to_the_earlier_kind():
    kinds = [n.kind for n in extract("on 28 September 2026 at 0900 hrs")]
    assert kinds == ["date", "time"]                           # "2026" is not also read as a time or a distance
    assert [n.kind for n in extract("0900 hrs")] == ["time"]    # not a 900-hour duration


def test_things_that_look_like_numbers_but_are_not():
    assert extract("Mairangi Bay") == [] and extract("5mins")[0].kind == "duration"
    assert [n.kind for n in extract("32 Gull Lane")] == []
    assert extract("31 February 2026") == []                    # impossible date is not invented
    # "2 8 September" is the text layer's broken "28 September": never read it as 8 September
    glitch = extract("2 8 September 2026 - 28 October 2026")
    assert [(n.kind, n.value) for n in glitch] == [("date", "2026-10-28")]


def test_the_numerics_tmp_reference_pattern_matches_the_config_pattern():
    cfg = load_config().furniture.tmp_ref_regex
    for ref in ("KTS 26-1581", "KTS26-1581", "AB 12-3456"):
        assert cfg.search(ref) and extract(ref)[0].kind == "tmp_reference"


@pytest.mark.parametrize("text,ftype,unit,expected", [
    ("RP 0.410", "rp", "km", (0.41, None)),
    ("9260", "integer", None, (9260, None)),
    ("3%", "percent", "%", (3.0, None)),
    ("5.409%", "percent", "%", (5.409, None)),
    ("37", "integer", None, (37, None)),
    ("9.9 m", "distance", "m", (9.9, None)),
    ("28 September 2026", "date", None, ("2026-09-28", None)),
    ("0900 hrs", "time", None, ("09:00", None)),
    ("4 shifts", "duration", None, (4, None)),
    ("Sunrise Avenue", "text", None, ("Sunrise Avenue", None)),
    ("  Murrays   Bay ", "text", None, ("Murrays Bay", None)),
    ("1,158", "integer", None, (1158, None)),
    ("50", "number", "km/h", (50, None)),
])
def test_parse_value_clean_typed_values(text, ftype, unit, expected):
    assert parse_value(text, ftype, unit) == expected


@pytest.mark.parametrize("text,ftype", [
    ("See Above", "date"), ("N/A", "integer"), ("", "integer"), ("Primary", "rp"),
    ("2 8 September 2026 - 28 October 2026", "date"), ("9.5", "integer"), ("abc", "percent"),
])
def test_parse_value_refuses_rather_than_guesses(text, ftype):
    value, problem = parse_value(text, ftype)
    assert value is None and problem
