from tmp_pipeline.config import load_config
from tmp_pipeline.layers.gridfind import detect_tables
from tmp_pipeline.layers.reader import read_layers

CFG = load_config()


def tables(pdf):
    return detect_tables(read_layers(pdf)[0], CFG)


def grid(c, x, y, cols, rows, cw=60, rh=20):
    c.setLineWidth(0.5)
    for i in range(rows + 1):
        c.line(x, y + i * rh, x + cols * cw, y + i * rh)
    for j in range(cols + 1):
        c.line(x + j * cw, y, x + j * cw, y + rows * rh)


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
