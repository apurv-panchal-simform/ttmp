"""Chunk metadata (plan 4.4), added after every chunk is built: document fields, zone and printed page, diagram ids,
route positions, scope, status and conflicts of the values used, boilerplate, access groups and exact-match codes."""
from __future__ import annotations

import hashlib
import re

from ..config import Config
from ..facts.model import Facts
from ..numerics import extract
from ..records import Status
from . import tokens
from .context import DocContext
from .model import Chunk

ORDER = {Status.CONFLICT: 3, Status.UNVERIFIED: 2, Status.NOT_APPLICABLE: 1}
TMD_CHUNKS = {"doc_card", "stage", "sheet_overview", "sheet_sign_schedule", "sheet_notes"}


def _codes(text: str) -> list[str]:
    out = []
    for n in extract(text, ("tmd", "rp", "phone", "tmp_reference")):
        if n.kind == "rp":
            out.append(f"RP {float(n.value):.3f}")
        else:
            out.append(str(n.value))
    out += re.findall(r"\bTMD-\d{2}\b", text)
    return list(dict.fromkeys(out))


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


DOC_FIELDS = ("version", "tmp_ref", "tmp_ref_aliases", "site_address", "suburb", "road_names", "client", "contractor",
              "rca", "designer", "work_start", "work_end", "approval_status")


def doc_fields(ctx: DocContext, facts: Facts) -> dict:
    """The document's own fields: on the doc card only (an index that filters every chunk by them copies them from it)."""
    return {"version": facts.revision, "tmp_ref": ctx.tmp_ref, "tmp_ref_aliases": ctx.tmp_ref_aliases,
            "site_address": ctx.site_address, "suburb": ctx.suburb, "road_names": ctx.road_names,
            **{k: ctx.parties.get(k) for k in ("client", "contractor", "rca", "designer")},
            "work_start": ctx.work_start, "work_end": ctx.work_end, "approval_status": ctx.approval_status}


def add_metadata(chunks: list[Chunk], facts: Facts, ctx: DocContext, cfg: Config,
                 access_groups: list[str] | None = None) -> None:
    groups = list(access_groups if access_groups is not None else cfg.chunks.default_access_groups)
    by_affected: dict[str, list[str]] = {}
    for c in facts.conflicts:
        for a in c.affected:
            by_affected.setdefault(a, []).append(c.conflict_id)
    mention_records: dict[str, list[str]] = {}
    for m in facts.mentions:
        mention_records.setdefault(m.source.record_id, []).append(m.mention_id)
    seen_text: dict[str, set[int]] = {}
    for ch in chunks:
        seen_text.setdefault(hashlib.sha256(_norm(ch.content).encode()).hexdigest(), set()).update(ch.metadata["pages"])
    for ch in chunks:
        md = ch.metadata
        pages = md["pages"]
        page = min(pages) if pages else 0
        statuses = [fv.status for p in ch.field_paths if (fv := facts.value(p)) is not None]
        worst = max(statuses, key=lambda s: ORDER.get(s, 0), default=Status.VERIFIED)
        keys = [*ch.field_paths, *ch.record_ids, *(i for r in ch.record_ids for i in mention_records.get(r, []))]
        tmd_ids = list(dict.fromkeys([*md.get("tmd_ids", []), *re.findall(r"\bTMD-\d{2}\b", ch.content)]))
        if ch.chunk_type in TMD_CHUNKS:
            keys += tmd_ids
        conflict_ids = sorted({cid for k in keys for cid in by_affected.get(k, [])} | set(md.get("conflict_ids", [])))
        rps = sorted({*md.get("rp_values", []),
                      *(float(n.value) for n in extract(ch.content, ("rp",)) if isinstance(n.value, int | float))})
        h = hashlib.sha256(_norm(ch.content).encode()).hexdigest()
        zones = {ctx.zones.get(p, "") for p in pages}
        md.pop("symbols", None)                  # the sheet's symbols live in its record; the sheet chunks say them in words
        if ch.chunk_type == "doc_card":          # document fields once, on the card (doc_info reads them from it)
            md.update(doc_fields(ctx, facts))
        md.update({
            "doc_id": ctx.doc_id,
            "zone": ctx.zones.get(page, ""), "page": page, "printed_page": ctx.printed.get(page),
            "tmd_ids": tmd_ids, "linked_sheets": md.get("linked_sheets") or [t for t in tmd_ids if facts.sheet(t)],
            "rp_from": rps[0] if rps else None, "rp_to": rps[-1] if rps else None, "rp_values": rps,
            "scope": md.get("scope") or ("generic" if zones == {"generic_standard"} else "site_specific"),
            "applicability": "active", "status": str(worst), "has_conflict": bool(conflict_ids),
            "conflict_ids": conflict_ids,
            "is_boilerplate": len(seen_text.get(h, set())) >= cfg.chunks.boilerplate_min_pages,
            "boilerplate_hash": h[:16], "access_groups": groups, "codes": _codes(ch.header + "\n" + ch.content),
        })
        ch.tokens = tokens.count(ch.text())
        ch.rehash()
