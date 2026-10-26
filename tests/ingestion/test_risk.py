"""Risk matrix and register on hand-built stores: matrix cells from geometry, the document's own colour key,
rating checked by cell lookup (not score), stacked register rows and TMD links."""
from helpers import CFG, add_block, add_table, line, store_with_page
from tmp_pipeline.ingestion.extract.handlers.risk import RiskTableHandler
from tmp_pipeline.ingestion.extract.handlers.stubs import stub_registry
from tmp_pipeline.ingestion.extract.router import route
from tmp_pipeline.records import RecordStore

RED, ORANGE, YELLOW, GREEN, LIGHT = (1.0, 0.0, 0.0), (0.965, 0.522, 0.165), (1.0, 1.0, 0.0), (0.0, 0.69, 0.31), (0.62, 0.98, 0.53)
YELLOW_2 = (1.0, 0.95, 0.0)           # the register's slightly different yellow


def fill(box, rgb):
    return {"bbox": list(box), "rgb": list(rgb)}


def with_key_and_matrix(store, page=1):
    """Key rows (swatch digit, name, WHS text, action) and a matrix with likelihood rows 4 and 2 and impact columns
    1, 2 and 4, so the rating of a score is not a function of the score: 4 is yellow at 4x1 but green at 2x2."""
    key_rows = [(5, "Very High", RED, "Fatality or permanent disability"), (4, "High", ORANGE, "Lost time injury or illness"),
                (3, "Medium", YELLOW, "Medical treatment injury"), (2, "Low", GREEN, "First Aid injury"),
                (1, "Insignificant", LIGHT, "No significant injury")]
    fills = []
    blocks = []
    for i, (lvl, name, rgb, whs) in enumerate(key_rows):
        y = 560 - 18 * i
        fills.append(fill((60, y, 85, y + 16), rgb))
        blocks += [line(str(lvl), 66, y + 3, 72, y + 12), line(name, 92, y + 3, 140, y + 12),
                   line(whs, 294, y + 3, 400, y + 12), line("Action text", 508, y + 3, 600, y + 12)]
    cols = {1: 208, 2: 284, 4: 409}
    rows = {4: 300, 2: 270}
    blocks += [line(f"{k} - {n}", cx - 25, 330, cx + 25, 339) for (k, n), cx in
               zip([(1, "Insignificant"), (2, "Low"), (4, "High")], cols.values(), strict=True)]
    blocks += [line("4 - Likely", 92, 300, 133, 309), line("2 - Unlikely", 92, 270, 133, 279)]
    cells = {(4, 1): (4, YELLOW), (4, 2): (8, YELLOW), (4, 4): (16, ORANGE), (2, 1): (2, LIGHT), (2, 2): (4, GREEN), (2, 4): (8, YELLOW)}
    for (lk, imp), (score, rgb) in cells.items():
        cx, y = cols[imp], rows[lk]
        fills.append(fill((cx - 30, y - 4, cx + 30, y + 13), rgb))
        blocks.append(line(str(score), cx - 3, y, cx + 3, y + 9))
    t = store.add(page, (842, 595), "risk", "table", [50, 255, 640, 580], source="test",
                  metadata={"h_rules": [[255, 50, 640], [580, 50, 640]], "v_rules": [[50, 255, 580], [640, 255, 580]],
                            "fills": fills, "n_xs": 2, "n_ys": 2, "n_parts": 4, "n_fills": len(fills), "rating_cells": 10})
    for b in blocks:
        add_block(store, [b], page=page, parent=t.element_id, zone="risk")
    return t


def register(store, rows, page=2):
    names = ["Risk category", "Hazard / trigger", "Initial likelihood", "Initial consequence", "Initial risk",
             "Residual likelihood", "Residual consequence", "Residual risk", "Affected TMD"]
    return add_table(store, 50, 540, [60, 90, 60, 90, 60, 60, 90, 60, 60], 40, [names, *rows], page=page, zone="risk",
                     header_rows=1)


def run(store):
    reg = stub_registry()
    reg.replace(RiskTableHandler())
    records = RecordStore(store.doc_id)
    res = route(store, CFG, registry=reg, records=records)
    assert not res.failures, res.failures
    return records


def base_store(rows):
    s = store_with_page(1)
    from tmp_pipeline.ingestion.models import PageInfo
    s.pages[2] = PageInfo(width_pt=842, height_pt=595)
    with_key_and_matrix(s)
    register(s, rows)
    return s


def matrix_of(records):
    (m,) = records.of_kind("risk_matrix")
    return m, {(c["likelihood"], c["impact"]): c for c in m.data["cells"]}


def test_matrix_cells_come_from_geometry_with_ratings_from_the_documents_own_key():
    m, cells = matrix_of(run(base_store([])))
    assert len(cells) == 6 and m.data["complete"] is False         # a subset: 6 of 25, so completeness is reported
    assert (cells[(4, 1)]["score"], cells[(4, 1)]["rating"]) == (4, "Medium")        # score 4 is yellow at 4 x 1
    assert (cells[(2, 2)]["score"], cells[(2, 2)]["rating"]) == (4, "Low")           # ...and green at 2 x 2
    assert cells[(4, 4)]["rating"] == "High" and cells[(2, 1)]["rating"] == "Insignificant"
    assert cells[(4, 1)]["colour"] == list(YELLOW) and cells[(4, 4)]["element_id"].startswith("p1:text_block")


def test_key_records_hold_swatch_name_whs_and_action():
    recs = run(base_store([]))
    key = {r.fields["rating"].value: r for r in recs.of_kind("risk_impact_key")}
    assert set(key) == {"Very High", "High", "Medium", "Low", "Insignificant"}
    assert key["Very High"].fields["impact"].value == 5 and key["Very High"].fields["whs"].value == "Fatality or permanent disability"
    assert key["Low"].data["rgb"] == list(GREEN)


def test_risk_cell_records_carry_score_rating_and_flag_a_wrong_score():
    recs = run(base_store([]))
    cells = {(r.fields["likelihood"].value, r.fields["impact"].value): r for r in recs.of_kind("risk_cell")}
    assert len(cells) == 6 and cells[(4, 4)].fields["score"].value == 16 and cells[(4, 4)].status == "verified"
    s = base_store([])
    for e in s.of_type("text_block"):
        if e.representations["text"] == "16":                       # the printed score disagrees with 4 x 4
            e.representations["lines"][0]["text"] = e.representations["text"] = "15"
    bad = {(r.fields["likelihood"].value, r.fields["impact"].value): r for r in run(s).of_kind("risk_cell")}
    assert bad[(4, 4)].status == "unverified" and "is not likelihood x impact (16)" in bad[(4, 4)].fields["score"].notes[0]


def row(cat, hz, il, ic, ir, rl, rc, rr, tmd):
    return [cat, hz, il, ic, ir, rl, rc, rr, tmd]


def test_the_stated_rating_is_checked_by_cell_lookup_and_a_mismatch_keeps_both_values():
    rows = [row("Work area", "Vehicle strikes worker", "Likely", "Lost time injury or illness", "Very High",
                "Unlikely", "First Aid injury", "Low", "TMD 02 / TMD 03"),
            row("Weather", "Rain", "Likely", "Lost time injury or illness", "High", "Unlikely", "First Aid injury", "Low", "TMD 04")]
    recs = run(base_store(rows))
    a, b = recs.of_kind("risk")
    # row a: 4 x 4 is orange = High in the matrix, but the register says Very High -> mismatch, not corrected
    assert "rating_mismatch" in a.flags and a.fields["initial_risk"].raw == "Very High"
    assert a.data["rating_checks"]["initial"] == [{"likelihood": 4, "impact": 4, "score": 16, "stated": "Very High", "matrix": "High"}]
    assert "rating_mismatch: stated Very High, matrix cell (likelihood 4, impact 4) says High" in a.fields["initial_risk"].notes
    # residual 2 x 2 is green = Low and the register says Low: fine; row b agrees everywhere
    assert a.data["rating_checks"]["residual"][0]["matrix"] == "Low" and "rating_mismatch" not in b.flags
    assert a.fields["residual_risk"].notes == []


def test_a_lookup_that_cannot_be_made_is_noted_not_guessed():
    rows = [row("Odd", "Something", "Sometimes", "Fatality or permanent disability", "Very High", "Unlikely", "First Aid injury", "Low", "")]
    (r,) = run(base_store(rows)).of_kind("risk")
    chk = r.fields["initial_check"]
    assert chk.value is None and chk.status == "unverified" and "could not pair" in chk.notes[0]
    assert "rating_mismatch" not in r.flags


def test_affected_tmd_column_becomes_diagram_links():
    rows = [row("Work area", "Hz", "Likely", "Lost time injury or illness", "High", "Unlikely", "First Aid injury", "Low",
                "TMD 02 / TMD 03 / TMD 04")]
    (r,) = run(base_store(rows)).of_kind("risk")
    assert r.fields["affected_tmds"].value == ["TMD-02", "TMD-03", "TMD-04"] and r.data["links"] == ["TMD-02", "TMD-03", "TMD-04"]
    assert r.fields["affected_tmds"].raw == "TMD 02 / TMD 03 / TMD 04" and r.fields["affected_tmds"].method == "pattern"


def test_stacked_rows_in_one_tall_cell_split_into_one_record_per_hazard():
    """Three hazards share tall merged cells (no rules between them); only the rating cells have their own rows."""
    names = ["Risk category", "Hazard / trigger", "Initial likelihood", "Initial consequence", "Initial risk",
             "Residual likelihood", "Residual consequence", "Residual risk", "Affected TMD"]
    s = store_with_page(1)
    from tmp_pipeline.ingestion.models import PageInfo
    s.pages[2] = PageInfo(width_pt=842, height_pt=595)
    with_key_and_matrix(s)
    rows = [names,
            [None, None, None, None, "High", "Unlikely", "First Aid injury", "Low", "TMD 02"],
            [None, None, None, None, "Very High", None, None, "Low", "TMD 04"]]
    t, xs, ys = add_table(s, 50, 540, [60, 90, 60, 90, 60, 60, 90, 60, 60], 40, rows, page=2, zone="risk", header_rows=1,
                          no_rule_below={(1, 0), (1, 1), (1, 2), (1, 3), (1, 5), (1, 6)})
    stacked = {0: ("Work area", "Stop Go"), 1: ("Hz A", "Hz B"), 2: ("Likely", "Very Likely"),
               3: ("Lost time injury or illness", "Lost time injury or illness")}
    for c, values in stacked.items():          # one value per row band inside the tall cell
        for r, text in enumerate(values, start=1):
            add_block(s, [line(text, xs[c] + 3, ys[r] - 10, xs[c + 1] - 3, ys[r] - 2)], page=2, parent=t.element_id, zone="risk")
    recs = run(s)
    risk = recs.of_kind("risk")
    assert len(risk) == 2
    # the stacked text cell is split by row band: each hazard keeps its own value
    assert [r.fields["initial_likelihood"].raw for r in risk] == ["Likely", "Very Likely"]
    assert [r.fields["hazard"].raw for r in risk] == ["Hz A", "Hz B"] and [r.fields["category"].raw for r in risk] == ["Work area", "Stop Go"]
    assert [r.fields["affected_tmds"].value for r in risk] == [["TMD-02"], ["TMD-04"]]


def test_a_missing_key_or_matrix_leaves_the_register_unchecked_without_failing():
    s = store_with_page(1)
    register(s, [row("Work area", "Hz", "Likely", "Lost time injury or illness", "High", "Unlikely", "First Aid injury", "Low", "TMD 02")], page=1)
    recs = run(s)
    (r,) = recs.of_kind("risk")
    assert r.fields["initial_risk"].raw == "High" and "rating_checks" not in r.data and not recs.of_kind("risk_matrix")


def test_records_are_stamped_and_evidence_points_at_the_source_elements():
    recs = run(base_store([]))
    m, _ = matrix_of(recs)
    assert m.produced_by.handler == "RiskTableHandler" and len(m.evidence) == 6 and m.evidence[0].fingerprint
    assert add_block is not None and line is not None
