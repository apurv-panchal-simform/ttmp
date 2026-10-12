"""Link records by id (plan section 3.2): facts to the diagram sheets their text names, figures to their sheet,
and every fact to the section it sits under. Links are fact ids or TMD ids, never copies."""
from __future__ import annotations

from ..models import ElementStore
from .model import Fact, Facts


def _top(store: ElementStore, fact: Fact) -> tuple[int, float] | None:
    """(page, top y) of the first element of a fact's first source."""
    for s in fact.sources:
        for eid in s.element_ids:
            el = store.elements.get(eid)
            if el is not None:
                return el.page, el.bbox_pt[3]
    return None                                     # page furniture and other sources without elements


def link_tmds(facts: Facts) -> None:
    by_record: dict[str, set[str]] = {}
    for m in facts.mentions:
        if m.mapped_to:
            by_record.setdefault(m.source.record_id, set()).add(m.mapped_to)
    sheet_pages = {s.page: s.tmd_id for s in facts.tmd_sheets}
    for f in facts.facts:
        if f.entity == "stage":                     # a stage list's table names every stage: use the stage's own id
            ids = {str(f.fields["tmd_id"].value)} if "tmd_id" in f.fields else set()
        else:
            ids = set().union(*(by_record.get(s.record_id, set()) for s in f.sources))
        if f.entity == "figure":
            ids |= {sheet_pages[s.page] for s in f.sources if s.page in sheet_pages}
        if ids:
            f.links["tmd"] = sorted(ids)


def link_sections(facts: Facts, store: ElementStore) -> None:
    """Each fact belongs to the nearest section heading above it on its page, else the last section before it."""
    heads = []
    for sec in facts.of("section"):
        pos = _top(store, sec)
        if pos is not None:
            heads.append((pos[0], pos[1], sec.fact_id))
    for f in facts.facts:
        if f.entity == "section":
            continue
        pos = _top(store, f)
        if pos is None:
            continue
        page, top = pos
        above = [h for h in heads if h[0] == page and h[1] >= top]
        if above:
            best = min(above, key=lambda h: h[1])
        else:
            earlier = [h for h in heads if h[0] < page]
            if not earlier:
                continue
            best = max(earlier, key=lambda h: (h[0], -h[1]))
        f.links["section"] = [best[2]]
