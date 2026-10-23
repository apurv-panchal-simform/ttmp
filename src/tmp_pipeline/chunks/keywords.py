"""Search keywords from the catalog, with no model (plan 4.6 spirit: synonyms drive code).

A reader may name a thing differently from the form: "speed limit" for the posted speed, "street" for the road name,
"single lane alternating" for the Stop Go diagram. The catalog already lists those names, so the chunk that states
the value carries them as keywords. Words the chunk already says are left out, so a keyword only adds vocabulary.
"""
from __future__ import annotations

from ..catalog import Catalog, normalise
from ..facts.model import Facts
from .model import Chunk

SHEET_CHUNKS = ("stage",)         # plus every sheet_* type: the chunks that are about one diagram sheet


def chunk_keywords(ch: Chunk, facts: Facts, catalog: Catalog, by_id: dict | None = None) -> list[str]:
    by_id = by_id if by_id is not None else {f.fact_id: f for f in facts.facts}
    said = f" {normalise(ch.header.split(chr(10), 1)[0] + ' ' + ch.content)} "
    out: dict[str, str] = {}

    def add(term: str) -> None:
        n = normalise(term)
        if n and n not in out and f" {n} " not in said:
            out[n] = term.strip()

    for path in ch.field_paths:
        fid, _, fname = path.rpartition(".")
        fact = by_id.get(fid)
        ent = (catalog.entities.get(fact.entity) or catalog.facts_entities.get(fact.entity)) if fact else None
        fd = ent.fields.get(fname) if ent else None
        if fd is not None:
            for term in fd.synonyms:          # the names the forms use, not the catalog's internal field name
                add(term)
    if ch.chunk_type in SHEET_CHUNKS or ch.chunk_type.startswith("sheet_"):
        for tid in ch.metadata.get("tmd_ids") or []:
            sheet = facts.sheet(tid)
            for term in catalog.diagram_title_synonyms.get(normalise(sheet.title), ()) if sheet else ():
                add(term)
    return list(out.values())
