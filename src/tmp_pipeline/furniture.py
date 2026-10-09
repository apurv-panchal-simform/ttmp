"""Page furniture and document metadata (plan sections 2.1 and 1.6).

Text in the header and footer bands that repeats on more than half the pages is removed from content and
its values (TMP reference, form name, printed page number) become metadata. A block also counts when it
matches a metadata pattern (the form name changes between forms, so it repeats on fewer than half the pages).
Content in the bands that does not repeat, such as a diagram sheet's title block, stays content.
"""
from __future__ import annotations

import re
from collections import Counter

from .logging_ import log_event
from .models import Element, ElementStore

_DIGITS = re.compile(r"\d+")


def _norm(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def _norm_ref(ref: str) -> str:
    """'KTS26-1581' / 'KTS  26-1581' -> 'KTS 26-1581'."""
    return re.sub(r"^([A-Z]+)\s*", r"\1 ", " ".join(ref.split()))


def _lines(text: str) -> list[str]:
    return [n for n in (_norm(l) for l in text.split("\n")) if n]


def _in_band(e: Element, cfg: dict, store: ElementStore) -> bool:
    h = store.pages[e.page]["height_pt"]
    cy = (e.bbox_pt[1] + e.bbox_pt[3]) / 2
    return cy > h - cfg["page"]["header_band_pt"] or cy < cfg["page"]["footer_band_pt"]


def apply_furniture(store: ElementStore, cfg: dict) -> None:
    f = cfg["furniture"]
    n_pages = len(store.pages)
    band_blocks = [e for e in store.of_type("text_block") if e.metadata["band"] in ("header", "footer")]

    pages_with: dict[tuple[str, str], set[int]] = {}
    for e in band_blocks:
        text = e.representations["text"]
        for key in {_norm(text), *_lines(text)}:
            pages_with.setdefault((e.metadata["band"], key), set()).add(e.page)

    def repeated(band: str, key: str) -> bool:
        return len(pages_with.get((band, key), ())) > f["min_page_share"] * n_pages

    patterns = [re.compile(f["form_name_regex"], re.I), re.compile(f["page_number_regex"], re.I)]
    kinds: dict[str, str] = {}
    for e in band_blocks:
        text, band = e.representations["text"], e.metadata["band"]
        flat = " ".join(text.split())
        lines = _lines(text)
        if repeated(band, _norm(text)):
            kinds[e.element_id] = "repeated"
        elif lines and sum(repeated(band, l) for l in lines) / len(lines) >= 0.5:
            kinds[e.element_id] = "repeated_lines"
        elif any(p.search(flat) for p in patterns):
            kinds[e.element_id] = "metadata_pattern"

    furniture_ids = set(kinds)
    # a header/footer decoration table holding only furniture is furniture too
    for t in store.of_type("table"):
        if not _in_band(t, cfg, store):
            continue
        kids = [e for e in store.elements.values() if e.parent_id == t.element_id]
        if all(k.element_id in furniture_ids for k in kids):
            furniture_ids.add(t.element_id)
            kinds[t.element_id] = "band_table"

    refs: Counter = Counter()
    ref_pat = re.compile(f["tmp_ref_regex"])
    form_pat, page_pat = re.compile(f["form_name_regex"], re.I), re.compile(f["page_number_regex"], re.I)
    form_done: set[int] = set()        # form name comes from the footer when there is one
    ordered = sorted(furniture_ids, key=lambda i: (store.elements[i].metadata.get("band") != "footer", i))
    for eid in ordered:
        e = store.elements[eid]
        text = " ".join(e.representations.get("text", "").split())
        e.metadata["furniture"] = kinds[eid]
        e.type = "page_furniture"
        for m in ref_pat.finditer(text):
            refs[_norm_ref(m.group(0))] += 1
            store.pages[e.page].setdefault("tmp_reference", _norm_ref(m.group(0)))
        m = form_pat.search(text)
        if m and e.page not in form_done:
            store.pages[e.page]["form_name"] = m.group(0)
            form_done.add(e.page)
        m = page_pat.search(text)
        if m:
            store.pages[e.page]["printed_page"] = int(m.group(1))
    for e in store.elements.values():
        e.printed_page = store.pages[e.page].get("printed_page")
    if refs:
        store.metadata["tmp_reference"] = refs.most_common(1)[0][0]
        store.metadata["tmp_reference_variants"] = sorted(refs)
    store.metadata["form_names"] = {str(p): i["form_name"] for p, i in store.pages.items() if i.get("form_name")}
    log_event("furniture", "done", elements=len(furniture_ids), tmp_reference=store.metadata.get("tmp_reference"),
              pages=n_pages)
