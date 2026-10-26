from tmp_pipeline.ingestion.extract.layers.quality import text_quality


def test_clean_text_scores_high():
    assert text_quality("Pedestrian Management") == 1.0
    assert text_quality("Stop/Go traffic control is in place on Sunrise Avenue") > 0.85


def test_broken_font_encoding_scores_low():
    assert text_quality("PĞ.Ğstrgan ManahĞmĞnt") < 0.2
    assert text_quality("Web: www.kiwi(cid:410)rafcsolutons.co.nz") < 0.6


def test_no_alphabetic_words_returns_none_not_zero():
    assert text_quality("") is None
    assert text_quality("0.410 50 12") is None
    assert text_quality("ab 1") is None          # words under 3 letters are ignored


def test_domain_abbreviations_count_as_words():
    assert text_quality("STMS TMD RCA AADT") == 1.0


def test_assess_reports_word_count_and_unmapped_glyph_markers():
    from tmp_pipeline.ingestion.extract.layers.quality import assess
    q = assess("Footpat(cid:346)REsco and (cid:286) more")
    assert q.n_cid == 2 and q.n_words >= 3
    assert q.is_low(0.6, 4) is True                  # markers make even a short block suspect
    names = assess("Dimple Manipon")
    assert names.score is not None and names.is_low(0.6, 4) is False   # too short to judge
    assert assess("").is_low(0.6, 4) is False
