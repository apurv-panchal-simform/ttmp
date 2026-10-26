import hashlib

from tmp_pipeline.ingestion.extract.intake import doc_dir, intake, mark_done


def _text_page(c, w, h):
    c.drawString(72, h - 72, "KTS 26-1581 hello world")


def _blank_page(c, w, h):
    c.rect(50, 50, 100, 100)  # drawing only, no text layer


def test_doc_id_is_sha256_of_bytes_and_stable(make_pdf, tmp_path):
    pdf = make_pdf("a.pdf", [_text_page])
    r1, r2 = intake(pdf, tmp_path / "out"), intake(pdf, tmp_path / "out")
    assert r1.doc_id == "sha256:" + hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert r1.doc_id == r2.doc_id
    assert r1.pages == 1 and r1.size_bytes == pdf.stat().st_size


def test_version_from_filename(make_pdf, tmp_path):
    pdf = make_pdf("TMP V2 (DOM-1) - 55 Sunrise Avenue.pdf", [_text_page])
    assert intake(pdf, tmp_path / "out").version == "V2"


def test_version_falls_back_to_title_block_then_none(make_pdf, tmp_path):
    def titled(c, w, h):
        c.drawString(72, h - 72, "TMP Reference KTS 26-1581  Version V3")
    assert intake(make_pdf("plain.pdf", [titled]), tmp_path / "out").version == "V3"
    assert intake(make_pdf("plain2.pdf", [_text_page]), tmp_path / "out").version is None


def test_text_free_pdf_is_flagged_scanned(make_pdf, tmp_path):
    r = intake(make_pdf("scan.pdf", [_blank_page, _blank_page]), tmp_path / "out")
    assert r.scanned is True
    assert r.pages_without_text == [1, 2]


def test_text_pdf_is_not_scanned(make_pdf, tmp_path):
    r = intake(make_pdf("t.pdf", [_text_page, _blank_page]), tmp_path / "out")  # 1 of 2 pages empty
    assert r.scanned is False and r.pages_without_text == [2]


def test_second_run_on_same_file_is_skipped(make_pdf, tmp_path):
    out = tmp_path / "out"
    pdf = make_pdf("a.pdf", [_text_page])
    first = intake(pdf, out)
    assert first.already_processed is False
    mark_done(out, first.doc_id)
    again = intake(pdf, out)
    assert again.already_processed is True
    assert doc_dir(out, first.doc_id).name == "sha256-" + first.doc_id.split(":")[1]
