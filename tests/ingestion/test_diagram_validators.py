"""Code checks on image and diagram model output: an invented number, a missing id or a far-away word is never trusted."""
from tmp_pipeline.ingestion import prompts
from tmp_pipeline.ingestion.validators import Word, agree_x9, check_x7, check_x8, check_x9, check_x10, x9_key

WORDS = {w.id: w for w in (
    Word("w1", "RHB 0.3M O.S", [10, 10, 60, 18]),
    Word("w2", "Main=100PE", [10, 30, 60, 38]),
    Word("far", "30 m", [900, 900, 950, 910]),
)}


def _x7(**over):
    base = {"kind": "site plan", "description": "A site plan.", "labels": [], "numbers": [], "legend": []}
    return {**base, **over}


def test_a_grounded_number_and_label_pass():
    r = _x7(numbers=[{"value": 0.3, "unit": "m", "meaning": "offset", "ocr_ids": ["w1"]}],
            labels=[{"text": "Main=100PE", "points_to": "pipe", "ocr_ids": ["w2"]}])
    assert check_x7(r, WORDS).ok


def test_an_invented_number_fails_even_when_it_cites_a_real_word():
    r = _x7(numbers=[{"value": 0.5, "unit": "m", "meaning": "offset", "ocr_ids": ["w1"]}])
    chk = check_x7(r, WORDS)
    assert not chk.ok and "do not hold this value" in chk.items[0].problems[0]


def test_a_cited_word_that_does_not_exist_or_is_far_away_fails():
    assert "does not exist" in check_x7(_x7(numbers=[{"value": 1, "ocr_ids": ["nope"]}]), WORDS).items[0].problems[0]
    r = _x7(numbers=[{"value": 0.3, "ocr_ids": ["w1", "far"]}])
    assert "not next to each other" in " ".join(check_x7(r, WORDS).items[0].problems)


def test_a_number_with_no_ocr_id_is_unverified_unless_the_pdf_text_has_it():
    r = _x7(numbers=[{"value": 35, "unit": "m", "ocr_ids": []}])
    assert not check_x7(r, WORDS).ok
    assert check_x7(r, WORDS, pdf_text_in_crop="Road 35 m wide").ok


def test_the_model_value_must_agree_with_real_pdf_text_in_the_crop():
    r = _x7(numbers=[{"value": 0.3, "ocr_ids": ["w1"]}])
    assert not check_x7(r, WORDS, pdf_text_in_crop="RHB 0.4M O.S").ok


def test_no_description_or_no_answer_fails():
    assert not check_x7(_x7(description=""), WORDS).ok and not check_x7(None, WORDS).ok


SYMS = {"s1": {"text": "TEMPORARY", "value": None, "rp": None}, "s2": {"text": "30", "value": 30, "rp": 0.615}}


def test_x8_claims_must_cite_existing_symbols_and_known_numbers():
    ok = {"approaches": [{"direction": "west", "signs_in_order": ["s1", "s2"], "notes": "30 km/h at RP 0.615"}],
          "layout": [{"statement": "Work area near house 55", "symbol_ids": ["s2"]}], "pedestrians": [], "unclear": []}
    assert check_x8(ok, SYMS, "house 55").verified
    bad = {"approaches": [{"direction": "west", "signs_in_order": ["s1", "s9"], "notes": "40 km/h"}],
           "layout": [{"statement": "A statement", "symbol_ids": []}], "pedestrians": [], "unclear": []}
    chk = check_x8(bad, SYMS)
    assert not chk.verified
    problems = [p for c in chk.claims["approaches"] + chk.claims["layout"] for p in c["problems"]]
    assert any("s9" in p for p in problems) and any("40 is not on the sheet" in p for p in problems)
    assert any("no symbol ids" in p for p in problems)


def test_x8_with_no_claims_or_no_answer_is_not_verified():
    assert not check_x8({"approaches": [], "layout": [], "pedestrians": []}, SYMS).verified
    assert not check_x8(None, SYMS).verified


def test_x10_unclear_low_confidence_and_invented_digits():
    assert check_x10({"caption": "keep-left arrow sign", "confidence": "high"}) == ("keep-left arrow sign", [])
    assert check_x10({"caption": "unclear", "confidence": "high"})[0] is None
    assert check_x10({"caption": "bus stop sign", "confidence": "low"})[1] == ["low confidence"]
    assert "number" in check_x10({"caption": "30 km/h sign", "confidence": "high"}, "")[1][0]
    assert check_x10({"caption": "30 km/h sign", "confidence": "high"}, "30")[1] == []


def test_x9_items_need_cited_words_and_both_reads_must_agree():
    r1 = {"distances": [{"value": 0.3, "unit": "m", "meaning": "x", "ocr_ids": ["w1"]}], "rps": [{"value": 9, "ocr_ids": []}],
          "signs": [{"text": "30", "ocr_ids": []}]}
    chk = check_x9(r1, WORDS)
    assert [(i.what, i.ok) for i in chk.items] == [("distances", True), ("rps", False), ("signs", False)]
    r2 = {"distances": [{"value": 0.30, "unit": "m", "ocr_ids": []}], "rps": [{"value": 8}]}
    assert agree_x9(r1, r2) == {("distances", "0.3")}
    assert x9_key("signs", "WORKS  END") == ("signs", "worksend")


def test_prompt_builders_are_deterministic_and_use_the_right_models_and_images():
    a = prompts.x8_sheet_summary(b"o", b"m", "04", "STOP GO", "green = Work Area", "s1 | x", "n")
    assert len(a.images) == 2 and a.model == prompts.MODELS["X8"] and a.key() == prompts.x8_sheet_summary(
        b"o", b"m", "04", "STOP GO", "green = Work Area", "s1 | x", "n").key()
    first = prompts.x7_read_image(b"i", "h", "n", "t", [("w1", "RHB")])
    second = prompts.x7_read_image(b"i", "h", "n", "t", [("w1", "RHB")], second_read=True)
    assert second.model == "gpt-5.5" and first.key() != second.key() and "w1: RHB" in second.user
    r2 = prompts.x9_read_region(b"i", "plan tile", "L", [("w1", "30 m")], second_read=True)
    assert r2.model == "gpt-5.5" and "w1" not in r2.user and "(none)" in r2.user      # the second read sees no word list
    assert prompts.x9_read_region(b"i", "plan tile", "L", [("w1", "30 m")]).user.count("w1: 30 m") == 1
    assert set(prompts.PROMPT_VERSIONS) >= {"X7", "X8", "X9", "X10"}


def test_numbers_in_symbol_text_never_break_on_times_and_ratios():
    from tmp_pipeline.ingestion.validators import _numbers, check_x12
    assert _numbers("NO PARKING 8:10 AM, 1:2:3, 8.10.2, 1,200") == {8.0, 10.0, 1.0, 2.0, 3.0, 8.1, 1200.0}
    assert check_x12({"summary": "Open 8:10 to 9:00."}, "Open 8:10 to 9:00.")[1] == []       # X12 still keeps 8:10 whole
    assert check_x12({"summary": "Open 8:10."}, "Open 8:15.")[1]


def test_symbol_ids_cited_in_a_summary_are_not_claimed_numbers():
    from tmp_pipeline.ingestion.validators import check_x8
    symbols = {"p22:vector_symbol:11": {"text": "30", "rp": 0.465}, "p22:vector_symbol:12": {"text": "50"}}
    result = {"approaches": [{"direction": "west", "signs_in_order": ["p22:vector_symbol:12", "p22:vector_symbol:11"],
                              "notes": "50 then 30 (IDs: p22:vector_symbol:12, p22:vector_symbol:11)."}],
              "layout": [], "pedestrians": []}
    chk = check_x8(result, symbols)
    assert chk.verified and chk.claims["approaches"][0]["problems"] == []
    result["approaches"][0]["notes"] = "40 then 30"
    assert not check_x8(result, symbols).verified


def test_a_sheet_summary_reaches_the_chunk_with_unchecked_claims_marked():
    from tmp_pipeline.ingestion.chunking.build import _sheet_summary
    summary = {"claims": {"layout": [{"ok": True, "statement": "Cones run along the footpath (p1:vector_symbol:3)."},
                                      {"ok": False, "statement": "A walkway is shown."}],
                          "approaches": [{"ok": False, "direction": "From the west", "notes": "50 first (IDs: p1:vector_symbol:1)."}],
                          "pedestrians": []},
               "unclear": ["Sign order cannot be read. [p1:vector_symbol:4]"]}
    assert _sheet_summary(summary) == [
        "Layout: Cones run along the footpath.", "Layout: A walkway is shown. (unconfirmed)",
        "Approach: From the west. 50 first. (unconfirmed)", "Not clear from the drawing: Sign order cannot be read."]
    assert _sheet_summary(None) == [] and _sheet_summary({"text": "T"}) == ["Summary: T"]
