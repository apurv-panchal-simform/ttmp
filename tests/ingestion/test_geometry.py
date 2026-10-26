import pytest

from tmp_pipeline.ingestion import geometry as g


def test_centre_and_area():
    assert g.centre([0, 0, 10, 20]) == (5, 10)
    assert g.area([0, 0, 10, 20]) == 200
    assert g.area([5, 5, 5, 9]) == 0


def test_centre_in_is_about_the_centre_not_overlap():
    assert g.centre_in([8, 8, 14, 14], [0, 0, 10, 10]) is False   # centre (11, 11) is outside
    assert g.centre_in([4, 4, 8, 8], [0, 0, 10, 10]) is True
    assert g.centre_in([0, 0, 10, 10], [0, 0, 10, 10]) is True    # edges count as inside


def test_contains_with_pad():
    assert g.contains([0, 0, 10, 10], [1, 1, 9, 9])
    assert not g.contains([0, 0, 10, 10], [-0.4, 1, 9, 9])
    assert g.contains([0, 0, 10, 10], [-0.4, 1, 9, 9], pad=0.5)


def test_iou():
    assert g.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1
    assert g.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0
    assert g.iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)
    assert g.iou([0, 0, 0, 0], [0, 0, 0, 0]) == 0


def test_union_and_empty():
    assert g.union([[0, 0, 5, 5], [3, -2, 9, 4]]) == [0, -2, 9, 5]
    with pytest.raises(ValueError):
        g.union([])


def test_smallest_containing_picks_tightest_box():
    inner = [4, 4, 6, 6]
    assert g.smallest_containing(inner, [[0, 0, 100, 100], [0, 0, 10, 10], [50, 50, 60, 60]]) == [0, 0, 10, 10]
    assert g.smallest_containing(inner, [[50, 50, 60, 60]]) is None


def test_in_band():
    h = 600
    assert g.in_band([0, 580, 10, 590], h, 62, 55) == "header"
    assert g.in_band([0, 10, 10, 20], h, 62, 55) == "footer"
    assert g.in_band([0, 300, 10, 310], h, 62, 55) is None


def test_pt_px_round_trip_on_a4_landscape():
    h = 595.28
    box = [100, 200, 300, 400]
    px = g.bbox_pt_to_px(box, h, 200)
    assert px[0] == pytest.approx(100 * 200 / 72)
    assert px[1] == pytest.approx((h - 400) * 200 / 72)       # y flips: top of the box is the smaller v
    assert g.bbox_px_to_pt(px, h, 200) == pytest.approx(box)
    assert g.rounded_px_box(box, h) == [round(v, 1) for v in px]
