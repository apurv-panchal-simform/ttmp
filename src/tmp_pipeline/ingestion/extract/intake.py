"""Intake (plan section 1.1): content hash as doc_id, version, scanned flag, skip-if-seen.

DOCX conversion (LibreOffice) is a Phase 8 platform job; intake takes a PDF.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf
import pypdf.errors

from ... import logging_ as lg
from ...config import IntakeCfg, load_config
from ...errors import CorruptPdfError, EmptyPdfError, EncryptedPdfError

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


def compute_doc_id(pdf_path: str | Path) -> str:
    """The content hash that names a document everywhere (sha256:<hex>)."""
    return "sha256:" + _sha256(Path(pdf_path))


def open_pdf(path: Path) -> pypdf.PdfReader:
    """Open a PDF for intake, turning library failures into typed errors."""
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise EncryptedPdfError(f"{path.name}: the PDF is password protected")
        if len(reader.pages) == 0:
            raise EmptyPdfError(f"{path.name}: the PDF has no pages")
    except (pypdf.errors.PyPdfError, ValueError, OSError, KeyError, TypeError, AttributeError) as e:
        if isinstance(e, FileNotFoundError):
            raise
        raise CorruptPdfError(f"{path.name}: not a readable PDF ({type(e).__name__}: {e})") from e
    return reader


def intake(pdf_path: str | Path, out_root: str | Path, cfg: IntakeCfg | None = None) -> IntakeResult:
    cfg = cfg or load_config().intake
    path = Path(pdf_path)
    doc_id = compute_doc_id(path)
    lg.set_doc_id(doc_id)
    reader = open_pdf(path)
    texts = [(p.extract_text() or "") for p in reader.pages]
    without = [i for i, t in enumerate(texts, 1) if len(t.strip()) < cfg.min_text_chars]
    scanned = bool(texts) and len(without) / len(texts) > cfg.scanned_share

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
