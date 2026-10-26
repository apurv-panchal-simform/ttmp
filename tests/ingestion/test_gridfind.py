from helpers import CFG, grid
from tmp_pipeline.ingestion.extract.layers.gridfind import detect_tables
from tmp_pipeline.ingestion.extract.layers.reader import read_layers


def tables(pdf):
    return detect_tables(read_layers(pdf)[0], CFG)


def test_ruled_grid_is_one_table_with_outer_bbox(make_pdf):
    def page(c, w, h):
        grid(c, 100, 100, 3, 4)
    (t,) = tables(make_pdf("g.pdf", [page]))
    assert t.bbox == [100, 100, 280, 180] or [round(v) for v in t.bbox] == [100, 100, 280, 180]
    assert t.n_xs == 4 and t.n_ys == 5


def test_single_rule_box_is_not_a_table(make_pdf):
    def page(c, w, h):
        grid(c, 100, 100, 1, 1, cw=200, rh=80)
    assert tables(make_pdf("b.pdf", [page])) == []


def test_two_separate_grids_are_two_tables(make_pdf):
    def page(c, w, h):
        grid(c, 50, 400, 2, 2)
        grid(c, 400, 100, 3, 3)
    assert len(tables(make_pdf("two.pdf", [page]))) == 2


def test_filled_cells_without_rules_make_a_table_and_count_rating_cells(make_pdf):
    def page(c, w, h):
        cols = [(1, 0, 0), (0.96, 0.52, 0.17), (1, 1, 0), (0, 0.69, 0.31), (0.62, 0.98, 0.53), (1, 0, 0)]
        for i, col in enumerate(cols):
            c.setFillColorRGB(*col)
            c.rect(100 + (i % 3) * 30, 100 + (i // 3) * 30, 30, 30, stroke=0, fill=1)
    (t,) = tables(make_pdf("f.pdf", [page]))
    assert t.rating_cells == 6


def test_nested_badge_inside_a_grid_is_absorbed(make_pdf):
    def page(c, w, h):
        grid(c, 100, 100, 3, 3)
        c.setFillColorRGB(0, 0.69, 0.31)
        c.rect(110, 110, 20, 10, stroke=0, fill=1)
        c.setStrokeColorRGB(0, 0, 0)
        grid(c, 112, 140, 2, 2, cw=10, rh=6)           # tiny grid wholly inside a cell
    ts = tables(make_pdf("n.pdf", [page]))
    assert len(ts) == 1


def test_rule_pieces_at_a_rounding_boundary_still_join_into_one_rule():
    from tmp_pipeline.ingestion.extract.layers.gridfind import _merge_rules
    # 438.75 / 1.5 is 292.5 and 438.45 / 1.5 is 292.3: a position-rounding key puts them in different clusters
    segs = [[50.0, 438.55, 113.22, 438.95], [113.62, 438.55, 202.65, 438.95], [202.7, 438.35, 300.0, 438.55]]
    h, v = _merge_rules(segs, CFG)
    assert len(h) == 1 and h[0][1] == 50.0 and h[0][2] == 300.0 and v == []
