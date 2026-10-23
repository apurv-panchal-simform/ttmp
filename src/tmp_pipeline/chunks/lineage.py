"""Lineage check (plan 4.5): after chunking, every canonical field is in a chunk's ``field_paths``; every section
(including ``other``) is in a chunk word for word; every informative image has a figure or sheet chunk; every
LEFTOVER element is in an ``other_section`` chunk; every record with text is carried by a chunk; every value of a
table's label/value pairs is written in some chunk (a record id on a chunk is not enough). Gaps are flagged, never
fixed silently."""
from __future__ import annotations

import re

from ..facts.model import Facts
from ..models import ElementStore
from ..records import Record, RecordStore
from ..vocab import Outcome
from . import tokens
from .model import Chunk


def _norm(text: str) -> str:
    return " ".join(text.split())


_WORD = re.compile(r"[a-z0-9]+(?:[./:-][a-z0-9]+)*")


class Corpus:
    """The text of a set of chunks, to ask whether a table's label/value pair is already written in one of them."""

    def __init__(self, texts: list[str]):
        self.text = _norm("\n".join(texts)).lower()
        self.words = set(_WORD.findall(self.text))

    def has_pair(self, label: str, value: str) -> bool:
        """A long value (8 words or more) counts when nearly all its words are written somewhere, since entity chunks
        reword it; a short one ("N/A", "0900 hrs") only when its label is written too, or it would match anywhere."""
        v = _norm(value.replace("☒", "").replace("☐", "")).lower()
        words = _WORD.findall(v)
        if not words:
            return True
        if len(words) >= 8:
            return v in self.text or sum(w in self.words for w in words) / len(words) >= 0.9
        lab = _norm(label).lower()
        if f"{lab}: {v}" in self.text:
            return True
        label_words = _WORD.findall(lab)
        return v in self.text and bool(label_words) and sum(w in self.words for w in label_words) / len(label_words) >= 0.75


def _has_text(rec: Record) -> bool:
    d = rec.data
    if rec.kind == "section":
        return bool(str(d.get("text") or "").strip())
    if d.get("pairs") or any(str(c.get("text") or "").strip() for c in d.get("cells") or []):
        return True
    return any(fv.value not in (None, "") or fv.raw for fv in rec.fields.values())


def check(chunks: list[Chunk], facts: Facts, records: RecordStore, store: ElementStore) -> dict:
    gaps: list[dict] = []
    paths = {p for c in chunks for p in c.field_paths}
    struck = [f.fact_id for f in facts.facts if f.applicability == "not_applicable"]
    for f in facts.facts:
        if f.applicability == "not_applicable":          # struck text is kept but never chunked (plan 4.1)
            continue
        for k in f.fields:
            if f"{f.fact_id}.{k}" not in paths:
                gaps.append({"kind": "field", "ref": f"{f.fact_id}.{k}", "pages": f.pages()})
    corpus = _norm("\n".join(c.content for c in chunks))
    sections = 0
    for r in records.of_kind("section"):
        if r.applicability == "not_applicable" or not _has_text(r):
            continue
        sections += 1
        for sentence in tokens.sentences(str(r.data.get("text") or "")):
            if _norm(sentence) not in corpus:
                gaps.append({"kind": "section_text", "ref": r.record_id, "pages": [r.page], "text": sentence[:80]})
                break
    images = 0
    carried = {r for c in chunks for r in c.record_ids}
    elements = {e for c in chunks for e in c.element_ids}
    for r in records.of_kind("figure", "diagram_sheet"):
        images += 1
        types = {c.chunk_type for c in chunks if r.record_id in c.record_ids}
        if not types & {"figure_caption", "sheet_overview", "sheet_image", "sheet_text", "sheet_layout"}:
            gaps.append({"kind": "image", "ref": r.record_id, "pages": [r.page]})
    other_elements = {e for c in chunks if c.chunk_type == "other_section" for e in c.element_ids}
    leftovers = [e for e in store.elements.values() if e.outcome == Outcome.LEFTOVER]
    for e in leftovers:
        if e.element_id not in other_elements:
            gaps.append({"kind": "leftover", "ref": e.element_id, "pages": [e.page]})
    tables = 0
    written = Corpus([c.content for c in chunks])
    for r in records.of_kind("table"):
        if r.applicability == "not_applicable" or not r.data.get("pairs"):
            continue
        tables += 1
        for p in r.data["pairs"]:
            if not written.has_pair(str(p.get("label") or ""), str(p.get("value") or "")):
                gaps.append({"kind": "table_row", "ref": r.record_id, "pages": [r.page],
                             "text": f"{p.get('label')}: {p.get('value')}"[:80]})
    exempt = []
    for r in records.records.values():
        if r.record_id in carried or (r.element_ids() and set(r.element_ids()) <= elements):
            continue
        if r.applicability == "not_applicable" or not _has_text(r):
            exempt.append(r.record_id)
            continue
        gaps.append({"kind": "record", "ref": r.record_id, "pages": [r.page]})
    n_fields = sum(len(f.fields) for f in facts.facts)
    return {"complete": not gaps, "fields": n_fields, "sections": sections, "images": images,
            "leftovers": len(leftovers), "tables": tables, "records": len(records), "exempt": sorted(exempt), "struck_facts": struck, "gaps": gaps}
