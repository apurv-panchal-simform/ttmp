from helpers import CFG
from tmp_pipeline.furniture import apply_furniture
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.to_elements import build_elements
from tmp_pipeline.zones import assign_zones


def run(pdf):
    s = build_elements(read_layers(pdf), "sha256:t", CFG)
    assign_zones(s, CFG)
    apply_furniture(s, CFG)
    return s


def page_with(n, form="1. TMP Planning Form", ref="KTS 26-1581", body="Body paragraph about the worksite plan"):
    def draw(c, w, h):
        c.setFont("Helvetica", 8)
        c.drawString(60, h - 30, f"TMP Reference {ref}")
        c.drawString(60, 25, f"New Zealand Guide to Temporary Traffic Management {form} Page {n}")
        c.setFont("Helvetica", 10)
        c.drawString(72, 300, f"{body} number {n}")
    return draw


def test_repeated_header_and_footer_are_furniture_and_promoted_to_metadata(make_pdf):
    s = run(make_pdf("f.pdf", [page_with(1), page_with(2), page_with(3)]))
    assert s.metadata["tmp_reference"] == "KTS 26-1581"
    assert [s.pages[p].printed_page for p in (1, 2, 3)] == [1, 2, 3]
    assert s.pages[1].form_name == "1. TMP Planning Form"
    furniture = s.of_type("page_furniture", page=1)
    assert furniture and all(e.metadata["band"] in ("header", "footer") for e in furniture if "band" in e.metadata)
    content = [e.representations["text"] for e in s.of_type("text_block", page=1)]
    assert content == ["Body paragraph about the worksite plan number 1"]
    assert all(e.printed_page == 1 for e in s.on_page(1))


def test_form_name_that_changes_halfway_is_still_furniture(make_pdf):
    pages = [page_with(1), page_with(2), page_with(3, form="2. TMP Job Pack Form"), page_with(4, form="2. TMP Job Pack Form"),
             page_with(5, form="3. TMP Regulatory Authorisation Form")]
    s = run(make_pdf("f.pdf", pages))
    assert s.of_type("text_block", page=5) and all("Form" not in e.representations["text"] for e in s.of_type("text_block", page=5))
    assert s.metadata["form_names"]["5"] == "3. TMP Regulatory Authorisation Form"


def test_non_repeating_footer_content_such_as_a_title_block_stays_content(make_pdf):
    def sheet(c, w, h):
        c.setFont("Helvetica", 8)
        c.drawString(60, 25, "SHEET NO: 01 REV NO: 00 DATE: 16/09/2026")
    s = run(make_pdf("t.pdf", [page_with(1), page_with(2), page_with(3), sheet]))
    texts = [e.representations["text"] for e in s.of_type("text_block", page=4)]
    assert texts == ["SHEET NO: 01 REV NO: 00 DATE: 16/09/2026"]
    assert not s.of_type("page_furniture", page=4)


def test_conflicting_tmp_references_are_both_kept_as_variants(make_pdf):
    s = run(make_pdf("c.pdf", [page_with(1), page_with(2), page_with(3, ref="KTS 26-1580")]))
    assert s.metadata["tmp_reference"] == "KTS 26-1581"
    assert s.metadata["tmp_reference_variants"] == ["KTS 26-1580", "KTS 26-1581"]


def test_header_band_table_pairs_become_header_fields(make_pdf):
    """TMP Reference | KTS 26-1581 AT-T | RCA consent and/or contract reference | AT-W | form name."""
    xs = [40, 140, 260, 420, 520, 700]

    def draw_page(n):
        def draw(c, w, h):
            top, bot = h - 18, h - 48
            c.setLineWidth(0.8)
            for x in xs:
                c.line(x, bot, x, top)
            c.line(xs[0], top, xs[-1], top)
            c.line(xs[0], bot, xs[-1], bot)
            c.setFont("Helvetica-Bold", 8)
            for x, lines in zip(xs, (["TMP Reference"], ["KTS 26-1581", "AT-T"], ["RCA consent and/or", "contract reference"],
                                     ["AT-W"], ["1. TMP PLANNING", "FORM"]), strict=False):
                for k, t in enumerate(lines):
                    c.drawString(x + 6, top - 12 - 10 * k, t)
            c.setFont("Helvetica", 8)
            c.drawString(60, 25, f"New Zealand Guide to Temporary Traffic Management 1. TMP Planning Form Page {n}")
            c.setFont("Helvetica", 10)
            c.drawString(72, 300, f"Body paragraph about the worksite plan number {n}")
        return draw
    s = run(make_pdf("band.pdf", [draw_page(1), draw_page(2), draw_page(3)]))
    fields = {f["label"]: f["value"] for f in s.metadata["header_fields"]}
    assert fields == {"TMP Reference": "KTS 26-1581 AT-T", "RCA consent and/or contract reference": "AT-W"}
    assert s.metadata["header_fields"][0]["pages"] == [1, 2, 3]
