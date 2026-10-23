"""Page furniture and document metadata (plan sections 2.1 and 1.6).

Text in the header and footer bands that repeats on more than half the pages is removed from content and
its values (TMP reference, form name, printed page number) become metadata. A block also counts when it
matches a metadata pattern (the form name changes between forms, so it repeats on fewer than half the pages).
Content in the bands that does not repeat, such as a diagram sheet's title block, stays content.
"""
from __future__ import annotations

import re
from collections import Counter

from .config import Config
from .geometry import in_band
from .logging_ import log_event
from .models import Element, ElementStore
from .vocab import ElementType

_DIGITS = re.compile(r"\d+")


def _norm(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def _norm_ref(ref: str) -> str:
    """'KTS26-1581' / 'KTS  26-1581' -> 'KTS 26-1581'."""
    return re.sub(r"^([A-Z]+)\s*", r"\1 ", " ".join(ref.split()))


def _lines(text: str) -> list[str]:
    return [n for n in (_norm(l) for l in text.split("\n")) if n]


def _in_band(e: Element, cfg: Config, store: ElementStore) -> bool:
    h = store.pages[e.page].height_pt
    return in_band(e.bbox_pt, h, cfg.page.header_band_pt, cfg.page.footer_band_pt) is not None


def _header_fields(store: ElementStore, band_tables: list[Element], cfg: Config) -> list[dict]:
    """Label/value pairs printed in a header band table, left to right ("TMP Reference | KTS 26-1581 AT-T | RCA
    consent and/or contract reference | AT-W"): the form name and page number are skipped, and a cell holding a
    TMP reference is never a label. Each distinct pair once, with the pages it is printed on, most pages first."""
    f = cfg.furniture
    found: dict[tuple[str, str], list[int]] = {}
    for t in band_tables:
        kids = sorted((e for e in store.elements.values() if e.parent_id == t.element_id
                       and str(e.representations.get("text") or "").strip()), key=lambda e: e.bbox_pt[0])
        texts = [" ".join(str(e.representations["text"]).split()) for e in kids]
        texts = [x for x in texts if not f.form_name_regex.search(x) and not f.page_number_regex.search(x)]
        i = 0
        while i + 1 < len(texts):
            label, value = texts[i], texts[i + 1]
            if f.tmp_ref_regex.search(label):
                i += 1
                continue
            found.setdefault((label, value), []).append(t.page)
            i += 2
    return [{"label": k[0], "value": k[1], "pages": sorted(set(p))}
            for k, p in sorted(found.items(), key=lambda kv: -len(set(kv[1])))]


def apply_furniture(store: ElementStore, cfg: Config) -> None:
    f = cfg.furniture
    n_pages = len(store.pages)
    band_blocks = [e for e in store.of_type("text_block") if e.metadata["band"] in ("header", "footer")]

    pages_with: dict[tuple[str, str], set[int]] = {}
    for e in band_blocks:
        text = e.representations["text"]
        for key in {_norm(text), *_lines(text)}:
            pages_with.setdefault((e.metadata["band"], key), set()).add(e.page)

    def repeated(band: str, key: str) -> bool:
        return len(pages_with.get((band, key), ())) > f.min_page_share * n_pages

    patterns = [f.form_name_regex, f.page_number_regex]
    kinds: dict[str, str] = {}
    for e in band_blocks:
        text, band = e.representations["text"], e.metadata["band"]
        flat = " ".join(text.split())
        lines = _lines(text)
        if repeated(band, _norm(text)):
            kinds[e.element_id] = "repeated"
        elif lines and sum(repeated(band, l) for l in lines) / len(lines) >= f.min_line_share:
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

    band_tables = [store.elements[i] for i, k in kinds.items() if k == "band_table"]   # ids change on retype
    refs: Counter = Counter()
    ref_pat = f.tmp_ref_regex
    form_pat, page_pat = f.form_name_regex, f.page_number_regex
    form_done: set[int] = set()        # form name comes from the footer when there is one
    ordered = sorted(furniture_ids, key=lambda i: (store.elements[i].metadata.get("band") != "footer", i))
    for eid in ordered:
        e = store.elements[eid]
        text = " ".join(e.representations.get("text", "").split())
        e.metadata["furniture"] = kinds[eid]
        store.retype(e, ElementType.PAGE_FURNITURE)
        for m in ref_pat.finditer(text):
            refs[_norm_ref(m.group(0))] += 1
            store.pages[e.page].tmp_reference = store.pages[e.page].tmp_reference or _norm_ref(m.group(0))
        fm = form_pat.search(text)
        if fm and e.page not in form_done:
            store.pages[e.page].form_name = fm.group(0)
            form_done.add(e.page)
        pm = page_pat.search(text)
        if pm:
            store.pages[e.page].printed_page = int(pm.group(1))
    for e in store.elements.values():
        e.printed_page = store.pages[e.page].printed_page
    header = _header_fields(store, band_tables, cfg)
    if header:
        store.metadata["header_fields"] = header
    if refs:
        store.metadata["tmp_reference"] = refs.most_common(1)[0][0]
        store.metadata["tmp_reference_variants"] = sorted(refs)
    store.metadata["form_names"] = {str(p): i.form_name for p, i in store.pages.items() if i.form_name}
    log_event("furniture", "done", elements=len(furniture_ids), tmp_reference=store.metadata.get("tmp_reference"),
              pages=n_pages)
