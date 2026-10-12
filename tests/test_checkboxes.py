"""Checkbox pairing, one test per layout in the plan's table (section 2.4). Stores are built by hand, so the
tests state exactly which text sits where, with no PDF or font involved."""
from helpers import add_block, add_box, line, run_handler, store_with_page
from tmp_pipeline.catalog import load_catalog
from tmp_pipeline.handlers.checkboxes import NOT_TICKED, CheckboxHandler


def run(store):
    records, result = run_handler(store, CheckboxHandler(), "CHECKBOX")
    assert result.coverage(store)["coverage"] == 1.0 and not result.failures
    return records


def only(records):
    (r,) = records.records.values()
    return r


def test_label_then_box_takes_the_label_on_its_left():
    s = store_with_page()
    add_block(s, [line("Pedestrians:", 89, 467, 139, 476)])
    add_box(s, 147, 466, ticked=True)
    r = only(run(s))
    assert (r.kind, r.entity, r.data["layout"], r.data["label"]) == ("checkbox", "vulnerable_user", "label_then_box", "Pedestrians:")
    assert r.fields["pedestrians"].value is True and r.data["answer"] == "ticked"
    assert [e.element_id for e in r.evidence][0].startswith("p1:checkbox")


def test_unticked_box_is_not_ticked_on_the_form_never_no():
    s = store_with_page()
    add_block(s, [line("Cyclists:", 106, 411, 139, 420)])
    add_box(s, 146, 410, ticked=False)
    r = only(run(s))
    assert r.fields["cyclists"].value is False and r.data["answer"] == NOT_TICKED == "not ticked on the form"


def test_row_of_pairs_each_box_takes_the_label_before_it():
    s = store_with_page()
    row = line("Controlled parking ☐ Traffic signals ☒ Rail corridor ☐", 86, 347, 788, 357)
    add_block(s, [row])
    for x, ticked in ((165, False), (402, True), (611, False)):
        add_box(s, x, 347, ticked, size=9)
    got = {r.data["label"]: (r.data["layout"], r.data["ticked"]) for r in run(s).records.values()}
    assert got == {"Controlled parking": ("row_of_pairs", False), "Traffic signals": ("row_of_pairs", True),
                   "Rail corridor": ("row_of_pairs", False)}


def test_yes_no_pair_is_one_answer_the_ticked_box_with_the_question_from_the_left():
    s = store_with_page()
    add_block(s, [line("Will bus stop(s) be obstructed by the work?", 155, 674, 327, 683)])
    add_block(s, [line("Yes ☒ No ☐", 440, 672, 510, 684)])
    add_box(s, 458, 672, True)
    add_box(s, 498, 672, False)
    records = run(s)
    r = only(records)
    assert (r.kind, r.data["question"], r.data["answer"]) == ("yes_no", "Will bus stop(s) be obstructed by the work?", "yes")
    assert list(r.fields.values())[0].value == "yes" and len(r.evidence) >= 3


def test_yes_no_answer_no_and_unanswered_cases():
    s = store_with_page()
    add_block(s, [line("Will signals be affected?", 155, 607, 300, 619)])
    add_block(s, [line("Yes ☐ No ☒", 440, 607, 510, 619)])
    add_box(s, 458, 607, False)
    add_box(s, 498, 607, True)
    add_block(s, [line("Is closure required?", 155, 507, 300, 519)])
    add_block(s, [line("Yes ☐ No ☐", 440, 507, 510, 519)])
    add_box(s, 458, 507, False)
    add_box(s, 498, 507, False)
    by_q = {r.data["question"]: r for r in run(s).records.values()}
    assert by_q["Will signals be affected?"].data["answer"] == "no"
    unanswered = by_q["Is closure required?"]
    assert unanswered.data["answer"] == "not answered on the form" and unanswered.status == "unverified"
    assert list(unanswered.fields.values())[0].value is None
    assert "neither box is ticked" in list(unanswered.fields.values())[0].notes


def test_lone_box_uses_the_whole_row_text_as_its_label():
    s = store_with_page()
    add_block(s, [line("Tick this box if you’re including a supplementary activity or other supporting information.",
                       55, 424, 443, 434, size=10)])
    add_box(s, 787, 422, False, size=16)
    r = only(run(s))
    assert r.data["layout"] == "lone_box" and r.data["label"].startswith("Tick this box if you’re including")
    assert r.fields[next(iter(r.fields))].value is False


def test_other_box_takes_the_free_text_beside_it_as_its_note():
    s = store_with_page()
    add_block(s, [line("Other:", 497, 389, 522, 398)])
    add_box(s, 529, 388, True)
    add_block(s, [line("Property access can be affected", 549, 389, 665, 398)])
    r = only(run(s))
    assert r.data["layout"] == "other_box" and r.data["note"] == "Property access can be affected"
    assert r.fields["other"].value is True and r.fields["other_note"].value == "Property access can be affected"


def test_box_with_a_short_note_keeps_the_note_with_the_value():
    s = store_with_page()
    add_block(s, [line("Aged care/mobility:", 445, 411, 522, 420)])
    add_block(s, [line("☐ Not present onsite", 530, 409, 617, 421)])
    add_box(s, 530, 409, False)
    r = only(run(s))
    assert (r.data["layout"], r.data["note"], r.data["ticked"]) == ("box_with_note", "Not present onsite", False)


def test_a_paragraph_beside_a_box_is_the_next_cell_not_its_note():
    s = store_with_page()
    add_block(s, [line("School &/or childcare:", 434, 467, 522, 476)])
    add_box(s, 529, 466, True)
    add_block(s, [line("No work will be undertaken within 50m of the school access half an", 549, 480, 804, 489),
                  line("hour of school start and finish where the", 549, 468, 804, 477)])
    r = only(run(s))
    assert r.data["note"] is None and r.data["layout"] == "label_then_box"


def test_a_box_with_no_text_on_its_row_is_unverified_not_guessed():
    s = store_with_page()
    add_box(s, 300, 300, True)
    r = only(run(s))
    assert r.data["layout"] == "unlabelled" and r.status == "unverified" and r.data["label"] == ""


def test_unknown_label_gets_a_snake_case_field_name_and_no_entity():
    s = store_with_page()
    add_block(s, [line("Over-dimension route:", 90, 300, 160, 309)])
    add_box(s, 165, 299, False)
    r = only(run(s))
    assert r.entity is None and "over_dimension_route" in r.fields


def test_box_ids_pages_and_stamp_are_recorded():
    s = store_with_page(2)
    add_block(s, [line("Bus route:", 98, 389, 139, 398)], page=2)
    add_box(s, 146, 388, True, page=2)
    r = only(run(s))
    assert r.page == 2 and r.produced_by.handler == "CheckboxHandler" and r.produced_by.config_hash
    assert r.produced_by.catalog_version == load_catalog().version and r.record_id == "r2:checkbox:0"
