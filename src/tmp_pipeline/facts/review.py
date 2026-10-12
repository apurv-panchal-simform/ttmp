"""Review list and reviewer corrections (plan section 3.3).

The document goes live with its flags; the review list holds what a person should look at: unverified values,
conflicts, rating mismatches, leftovers, unmatched diagram mentions and unsigned required blocks. A corrections
file confirms or corrects values; each becomes ``reviewed`` with the original kept, the facts revision is bumped
and every correction is appended to ``corrections.jsonl``. The fact paths it touched are returned so later phases
can rebuild only the affected chunks.

Corrections file::

    {"reviewer": "A. Person",
     "items": [{"ref": "road:0.start_rp", "action": "correct", "value": 0.41, "note": "checked against p4"},
               {"ref": "C-001", "action": "correct", "value": "KTS 26-1581"},
               {"ref": "m3:4", "action": "confirm"}]}
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import PipelineError
from ..models import atomic_write_text
from ..records import Status
from .model import Facts, ReviewItem, review_json


class CorrectionError(PipelineError):
    """A corrections file names something that does not exist or asks for an unknown action."""


def build_review(facts: Facts) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    in_conflict = {p for c in facts.conflicts for p in c.affected}
    for f in facts.facts:
        for fname, fv in f.fields.items():
            path = f"{f.fact_id}.{fname}"
            if fv.status == Status.UNVERIFIED and path not in in_conflict:
                items.append(ReviewItem(f"unverified:{path}", "unverified", path,
                                        "; ".join(fv.notes) or "not verified", sorted({s.page for s in fv.sources}),
                                        [fv.value if fv.value is not None else fv.raw]))
    for c in facts.conflicts:
        pages = sorted({p for v in c.versions for p in v.get("pages", [])})
        items.append(ReviewItem(f"conflict:{c.conflict_id}", "conflict", c.conflict_id, c.title, pages,
                                [v.get("value") for v in c.versions]))
    for f in facts.of("risk"):
        if "rating_mismatch" in f.flags:
            notes = [n for fv in f.fields.values() for n in fv.notes if n.startswith("rating_mismatch")]
            items.append(ReviewItem(f"rating_mismatch:{f.fact_id}", "rating_mismatch", f.fact_id,
                                    "; ".join(notes) or "rating disagrees with the matrix", f.pages()))
    for eid in facts.coverage.get("leftover", []):
        items.append(ReviewItem(f"leftover:{eid}", "leftover", eid, "no handler claimed this element",
                                [int(eid[1:].split(":")[0])] if eid[1:].split(":")[0].isdigit() else []))
    for m in facts.mentions:
        if m.mapped_to is None:
            items.append(ReviewItem(f"unmatched_mention:{m.mention_id}", "unmatched_mention", m.mention_id,
                                    "; ".join(m.notes) or "matches no diagram sheet", [m.page], [m.text]))
    if facts.approval is not None:
        for s in facts.signoffs:
            if s.role in facts.approval.unsigned:
                items.append(ReviewItem(f"unsigned:{s.role}", "unsigned", f"signoff:{s.role}",
                                        "; ".join(s.notes) or "not signed", [s.page], [s.signature]))
    return items


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stamp(reviewer: str, action: str, note: str | None, original: Any) -> dict:
    return {"reviewer": reviewer, "at": _now(), "action": action, "note": note, "original": original}


def apply_corrections(facts: Facts, corrections: dict) -> list[str]:
    """Apply a corrections document in place; returns the fact paths, conflict and mention ids it touched."""
    reviewer = str(corrections.get("reviewer") or "").strip()
    if not reviewer:
        raise CorrectionError("corrections need a reviewer")
    touched: list[str] = []
    for i, item in enumerate(corrections.get("items") or []):
        ref, action = str(item.get("ref") or ""), item.get("action")
        if action not in ("confirm", "correct"):
            raise CorrectionError(f"items[{i}]: action must be confirm or correct, got {action!r}")
        if action == "correct" and "value" not in item:
            raise CorrectionError(f"items[{i}]: a correction needs a value")
        note = item.get("note")
        conflict = next((c for c in facts.conflicts if c.conflict_id == ref), None)
        mention = next((m for m in facts.mentions if m.mention_id == ref), None)
        if conflict is not None:
            conflict.review = _stamp(reviewer, action, note, None)
            if action == "correct":
                conflict.review["value"] = item["value"]
            for path in conflict.affected:
                fv = facts.value(path) if "." in path else None
                if fv is not None:
                    fv.review = _stamp(reviewer, action, note, {"value": fv.value, "status": str(fv.status)})
                    if action == "correct":
                        fv.value = item["value"]
                    fv.status = Status.REVIEWED
                    touched.append(path)
            touched.append(ref)
        elif mention is not None:
            original = {"mapped_to": mention.mapped_to, "status": str(mention.status)}
            if action == "correct":
                if facts.sheet(str(item["value"])) is None:
                    raise CorrectionError(f"items[{i}]: {item['value']!r} is not a diagram sheet of this document")
                mention.mapped_to = str(item["value"])
                mention.method = "reviewed"
            mention.status = Status.REVIEWED
            mention.notes.append(f"reviewed by {reviewer}: {original}")
            touched.append(ref)
        else:
            fv = facts.value(ref)
            if fv is None:
                raise CorrectionError(f"items[{i}]: {ref!r} names no value, conflict or mention")
            fv.review = _stamp(reviewer, action, note, {"value": fv.value, "status": str(fv.status)})
            if action == "correct":
                fv.value = item["value"]
            fv.status = Status.REVIEWED
            touched.append(ref)
    done = set(touched)
    for r in facts.review:
        if r.ref in done:
            r.status = "closed"
    facts.revision += 1
    return touched


def apply_corrections_file(doc_dir: str | Path, corrections_path: str | Path) -> list[str]:
    doc = Path(doc_dir)
    facts = Facts.load(doc / "facts.json")
    corrections = json.loads(Path(corrections_path).read_text(encoding="utf-8"))
    touched = apply_corrections(facts, corrections)
    facts.dump(doc / "facts.json")
    atomic_write_text(doc / "review.json", review_json(facts))
    log = doc / "corrections.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": _now(), "revision": facts.revision, "reviewer": corrections.get("reviewer"),
                             "items": corrections.get("items"), "affected": touched}, ensure_ascii=False) + "\n")
    return touched
