"""Intake (plan section 1.1): content hash as doc_id, version, scanned flag, skip-if-seen.

DOCX conversion (LibreOffice) is a Phase 8 platform job; intake takes a PDF.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

from . import logging_ as lg

MIN_TEXT_CHARS = 20          # fewer characters than this = no usable text layer on the page
SCANNED_SHARE = 0.5          # scanned=true when more than this share of pages has no text layer
_FILENAME_VERSION = re.compile(r"(?<![A-Za-z0-9])V(\d+)(?![A-Za-z0-9])")
_TITLE_VERSION = re.compile(r"\b(?:Version\s*:?\s*)?V(\d+)\b|\bVersion\s*:?\s*(\d+)\b")
DONE_MARKER = ".done"


@dataclass
class IntakeResult:
    path: Path
    doc_id: str
    version: str | None
    pages: int
    size_bytes: int
    scanned: bool
    pages_without_text: list[int] = field(default_factory=list)
    already_processed: bool = False


def doc_dir(out_root: str | Path, doc_id: str) -> Path:
    return Path(out_root) / doc_id.replace(":", "-")


def mark_done(out_root: str | Path, doc_id: str) -> None:
    d = doc_dir(out_root, doc_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / DONE_MARKER).write_text("")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def intake(pdf_path: str | Path, out_root: str | Path) -> IntakeResult:
    path = Path(pdf_path)
    doc_id = "sha256:" + _sha256(path)
    lg.set_doc_id(doc_id)
    reader = pypdf.PdfReader(str(path))
    texts = [(p.extract_text() or "") for p in reader.pages]
    without = [i for i, t in enumerate(texts, 1) if len(t.strip()) < MIN_TEXT_CHARS]
    scanned = bool(texts) and len(without) / len(texts) > SCANNED_SHARE

    m = _FILENAME_VERSION.search(path.name)
    version = f"V{m.group(1)}" if m else None
    if version is None:
        for t in texts[:2]:
            m = _TITLE_VERSION.search(t)
            if m:
                version = f"V{m.group(1) or m.group(2)}"
                break

    seen = (doc_dir(out_root, doc_id) / DONE_MARKER).exists()
    res = IntakeResult(path, doc_id, version, len(texts), path.stat().st_size, scanned, without, seen)
    lg.log_event("intake", "done", file=path.name, bytes=res.size_bytes, pages=res.pages, version=version,
                 scanned=scanned, pages_without_text=without, already_processed=seen)
    return res
