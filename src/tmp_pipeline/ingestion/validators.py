"""Code-side checks on model output (plan part 2). The model proposes; these decide what may be trusted.

Nothing here calls a model. A failed check never edits a value: it returns problems, and the handler stores
the value as ``unverified`` with those problems as notes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import Catalog, FieldDef, normalise
from .numerics import extract


def _squash(text: str) -> str:
    return " ".join(text.split())


def check_quote(section_text: str, quote: str) -> bool:
    """The quote must appear word for word in the stored text (whitespace differences ignored)."""
    q = _squash(quote)
    return bool(q) and q in _squash(section_text)


def value_in_quote(value: object, quote: str, fd: FieldDef | None = None) -> bool:
    """The value must be inside its quote: as typed numerics that equal it, or as text."""
    q = _squash(quote)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        if any(isinstance(n.value, int | float) and float(n.value) == float(value) for n in extract(q)):
            return True
        # a bare number: match it as a whole token, so 25 is not found inside 250 or 2.5
        digits = re.escape(f"{value:g}" if isinstance(value, float) else str(value))
        return re.search(rf"(?<![\d.]){digits}(?![\d]|\.\d)", q.replace(",", "")) is not None
    return _squash(str(value)).lower() in q.lower()


def unit_matches(value: object, unit: str | None, quote: str, fd: FieldDef | None) -> bool:
    """The unit next to the number in the quote must be the field's unit (a '5 hours' quote is not minutes)."""
    expected = fd.unit if fd else None
    if expected is None or not isinstance(value, int | float) or isinstance(value, bool):
        return True
    if unit and unit.lower() != expected.lower():
        return False
    hits = [n for n in extract(_squash(quote)) if isinstance(n.value, int | float) and float(n.value) == float(value)]
    return not hits or any((n.unit or "").lower() == expected.lower() for n in hits)


@dataclass
class FactCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)


def check_fact(section_text: str, fact: dict, fd: FieldDef | None) -> FactCheck:
    """All three code checks for one X5 fact: quote verbatim, value in quote, unit matches."""
    problems = []
    quote = str(fact.get("quote") or "")
    if not check_quote(section_text, quote):
        problems.append("quote not found word for word in the section text")
    if not value_in_quote(fact.get("value"), quote, fd):
        problems.append("value does not appear in its quote")
    if not unit_matches(fact.get("value"), fact.get("unit"), quote, fd):
        problems.append("unit in the quote does not match the field's unit")
    return FactCheck(not problems, problems)


def check_x6(result: dict | None) -> tuple[bool, str]:
    """(supported, reason). Anything but a literal true is 'not supported'."""
    if not isinstance(result, dict) or result.get("supported") is not True:
        reason = result.get("reason") if isinstance(result, dict) else None
        return False, str(reason or "judge did not confirm the claim")
    return True, str(result.get("reason") or "")


def check_x4(result: dict | None, catalog: Catalog) -> tuple[str | None, str | None, str]:
    """(section_type or None, suggested_name, confidence). A type not in the catalog becomes 'other'."""
    if not isinstance(result, dict):
        return None, None, "low"
    st = result.get("section_type")
    conf = str(result["confidence"]) if result.get("confidence") in ("high", "medium", "low") else "low"
    if isinstance(st, str) and st in catalog.sections:
        return st, None, conf
    suggested = result.get("suggested_name")
    return None, suggested if isinstance(suggested, str) else None, conf


@dataclass
class NameMapping:
    entity: str
    entity_confident: bool
    mapped: dict[str, tuple[str, bool]]      # name -> (catalog field, confident)
    other: dict[str, str | None]             # name -> suggested snake_case field, for OTHER
    problems: list[str] = field(default_factory=list)


def check_x2(result: dict | None, catalog: Catalog, names: list[str]) -> NameMapping | None:
    """Validate an X2 mapping: the entity and every field must exist in the catalog, otherwise OTHER; names
    the model skipped become OTHER; low confidence is carried so the handler stores those values unverified."""
    if not isinstance(result, dict):
        return None
    entity = result.get("entity")
    if entity not in catalog.entities:
        return NameMapping("unknown", False, {}, {n: None for n in names}, ["entity is not in the catalog"])
    mapped: dict[str, tuple[str, bool]] = {}
    other: dict[str, str | None] = {}
    problems: list[str] = []
    seen_fields: set[str] = set()
    by_norm = {normalise(n): n for n in names}
    suggestions = {normalise(u.get("name", "")): u.get("suggested_field")
                   for u in result.get("unmapped", []) if isinstance(u, dict)}
    for m in result.get("mapping", []):
        if not isinstance(m, dict):
            continue
        name = by_norm.get(normalise(str(m.get("name", ""))))
        if name is None:
            problems.append(f"mapping names {m.get('name')!r}, which is not one of the table's names")
            continue
        fname, confident = m.get("field"), m.get("confidence") in ("high", "medium")
        if fname in catalog.entities[entity].fields and fname not in seen_fields:
            mapped[name] = (fname, confident)
            seen_fields.add(fname)
        else:
            if fname not in ("OTHER", None) and fname not in catalog.entities[entity].fields:
                problems.append(f"{name!r}: field {fname!r} is not in the catalog for {entity}")
            other[name] = suggestions.get(normalise(name))
    for n in names:
        if n not in mapped and n not in other:
            other[n] = suggestions.get(normalise(n))
    return NameMapping(entity, result.get("entity_confidence") in ("high", "medium"), mapped, other, problems)


# --- images and diagrams (plan sections 2.6 and 2.7) ----------------------------------------------------------------
# A vision model reads pixels; code decides what may be trusted. Every number a model reports must be cited to OCR
# words (or PDF text lines) that exist, hold that number, and sit together; a statement about a diagram must cite
# symbols that exist and use only numbers the sheet's own extraction contains.

NEAR_PT = 80.0                         # cited words further apart than this do not describe one thing
_NUM = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")


@dataclass
class Word:
    """A word or line with an id the model can cite: a DI word, or a line of the PDF text layer."""
    id: str
    text: str
    bbox: list[float]


@dataclass
class ItemCheck:
    what: str                          # label | number | sign | distance | rp | ...
    value: object
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class ImageCheck:
    items: list[ItemCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(i.ok for i in self.items)


def _numbers(text: str) -> set[float]:
    return {float(m) for m in _NUM.findall(text.replace(",", ""))}


def _norm(text: str) -> str:
    return "".join(str(text).split()).lower()


def _gap(a: list[float], b: list[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


def _cited(ids: object, words: dict[str, Word]) -> tuple[list[Word], list[str]]:
    problems: list[str] = []
    if not isinstance(ids, list):
        return [], ["ocr_ids is not a list"]
    got = []
    for i in ids:
        w = words.get(str(i))
        if w is None:
            problems.append(f"cited word {i!r} does not exist")
        else:
            got.append(w)
    for a in got:
        if any(_gap(a.bbox, b.bbox) > NEAR_PT for b in got):
            problems.append("cited words are not next to each other")
            break
    return got, problems


def _holds_number(value: object, cited: list[Word]) -> bool:
    try:
        v = float(value)                                    # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _norm(str(value)) in _norm(" ".join(w.text for w in cited))
    return any(v in _numbers(w.text) for w in cited)


def check_item(what: str, value: object, ids: object, words: dict[str, Word], pdf_text: str = "",
               is_number: bool = True) -> ItemCheck:
    """One reported value: its cited words exist, are together, hold the value, and agree with real PDF text."""
    cited, problems = _cited(ids, words)
    if not cited and not problems:
        in_pdf = bool(pdf_text) and (_holds_number(value, [Word("", pdf_text, [0, 0, 0, 0])]) if is_number
                                      else _norm(str(value)) in _norm(pdf_text))
        if not in_pdf:
            problems.append("not in the OCR word list: read from pixels only")
    elif cited:
        holds = _holds_number(value, cited) if is_number else _norm(str(value)) in _norm(" ".join(w.text for w in cited))
        if not holds:
            problems.append("the cited words do not hold this value")
    if pdf_text and is_number and not _holds_number(value, [Word("", pdf_text, [0, 0, 0, 0])]) and not problems:
        problems.append("the value is not in the PDF text inside the image")
    return ItemCheck(what, value, not problems, problems)


def check_x7(result: dict | None, words: dict[str, Word], pdf_text_in_crop: str = "") -> ImageCheck:
    """Check an X7 reading: labels and numbers must be grounded in cited words; a description is required."""
    chk = ImageCheck()
    if not isinstance(result, dict):
        chk.problems.append("no usable answer")
        return chk
    if not str(result.get("description") or "").strip():
        chk.problems.append("no description")
    for lab in result.get("labels") or []:
        if isinstance(lab, dict):
            chk.items.append(check_item("label", lab.get("text"), lab.get("ocr_ids", []), words, pdf_text_in_crop, False))
    for num in result.get("numbers") or []:
        if isinstance(num, dict):
            chk.items.append(check_item("number", num.get("value"), num.get("ocr_ids", []), words, pdf_text_in_crop))
    return chk


def check_x9(result: dict | None, words: dict[str, Word]) -> ImageCheck:
    """Check an X9 region reading: every number-bearing item must cite words that exist and hold it."""
    chk = ImageCheck()
    if not isinstance(result, dict):
        chk.problems.append("no usable answer")
        return chk
    for key in ("distances", "rps", "cones"):
        for it in result.get(key) or []:
            if isinstance(it, dict):
                chk.items.append(check_item(key, it.get("value", it.get("spacing")), it.get("ocr_ids", []), words))
    for it in result.get("signs") or []:
        if isinstance(it, dict) and _numbers(str(it.get("text") or "")):
            chk.items.append(check_item("signs", it.get("text"), it.get("ocr_ids", []), words, "", False))
    return chk


def x9_key(what: str, value: object) -> tuple[str, str]:
    """How two X9 reads are compared: numbers as numbers, sign text without spaces or case."""
    if what == "signs":
        return what, _norm(str(value))
    try:
        return what, f"{float(value):g}"                      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return what, _norm(str(value))


def agree_x9(read1: dict | None, read2: dict | None) -> set[tuple[str, str]]:
    """The (kind, value) pairs both independent reads reported: only these can be verified."""
    def pairs(r: dict | None) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for key in ("distances", "rps", "cones", "signs"):
            for it in (r or {}).get(key) or []:
                if isinstance(it, dict):
                    out.add(x9_key(key, it.get("text") if key == "signs" else it.get("value", it.get("spacing"))))
        return out
    return pairs(read1) & pairs(read2)


@dataclass
class SummaryCheck:
    verified: bool
    problems: list[str] = field(default_factory=list)
    claims: dict[str, list[dict]] = field(default_factory=dict)      # section -> [{"ok", "problems", ...}]


_SYMBOL_ID = re.compile(r"p\d+:[a-z_]+:\d+")          # the ids the model cites (p22:vector_symbol:11): not claimed numbers


def check_x8(result: dict | None, symbols: dict[str, dict], extra_text: str = "") -> SummaryCheck:
    """An X8 summary may only cite existing symbols and only use numbers the sheet's own extraction contains."""
    if not isinstance(result, dict):
        return SummaryCheck(False, ["no usable answer"])
    allowed: set[float] = _numbers(extra_text)
    for s in symbols.values():
        allowed |= _numbers(str(s.get("text") or "")) | _numbers(str(s.get("value") if s.get("value") is not None else ""))
        if s.get("rp") is not None:
            allowed.add(float(s["rp"]))
    out: dict[str, list[dict]] = {"approaches": [], "layout": [], "pedestrians": []}
    problems: list[str] = []

    def judge(ids: object, text: str) -> list[str]:
        p: list[str] = []
        if not isinstance(ids, list) or not ids:
            p.append("no symbol ids cited")
        else:
            p += [f"symbol {i!r} does not exist" for i in ids if str(i) not in symbols]
        p += [f"number {n:g} is not on the sheet" for n in sorted(_numbers(_SYMBOL_ID.sub(" ", text)) - allowed)]
        return p

    for a in result.get("approaches") or []:
        if isinstance(a, dict):
            p = judge(a.get("signs_in_order"), str(a.get("notes") or ""))
            out["approaches"].append({"ok": not p, "problems": p, **a})
    for key in ("layout", "pedestrians"):
        for c in result.get(key) or []:
            if isinstance(c, dict):
                p = judge(c.get("symbol_ids"), str(c.get("statement") or ""))
                out[key].append({"ok": not p, "problems": p, **c})
    bad = [c for v in out.values() for c in v if not c["ok"]]
    if not any(out.values()):
        problems.append("the summary has no claims")
    return SummaryCheck(not bad and not problems, problems, out)


def check_x10(result: dict | None, symbol_text: str = "") -> tuple[str | None, list[str]]:
    """(caption, problems): "unclear" or low confidence is never a caption, and digits must come from the symbol."""
    if not isinstance(result, dict):
        return None, ["no usable answer"]
    caption = _squash(str(result.get("caption") or ""))
    if not caption or caption.lower() == "unclear":
        return None, ["the model could not identify the symbol"]
    problems = []
    if result.get("confidence") not in ("high", "medium"):
        problems.append("low confidence")
    if _numbers(caption) - _numbers(symbol_text):
        problems.append("the caption has a number the symbol does not show")
    return caption, problems


def check_x11(result: dict | None, sheet_ids: list[str]) -> tuple[str | None, list[str]]:
    """(tmd_id or None, problems): the id must be one of the document's sheets; "none" and low confidence leave the
    mention unmatched, and a medium-confidence match is kept but not verified."""
    if not isinstance(result, dict):
        return None, ["no usable answer"]
    tid = str(result.get("tmd_id") or "").strip()
    if not tid or tid.lower() == "none":
        return None, ["the model found no sheet that fits"]
    if tid not in sheet_ids:
        return None, [f"the model named {tid!r}, which is not a sheet of this document"]
    conf = result.get("confidence")
    if conf not in ("high", "medium"):
        return None, ["low confidence"]
    return tid, ([] if conf == "high" else ["medium confidence"])


_SUMMARY_NUM = re.compile(r"\d+(?:[.,:]\d+)*")          # whole figures like 8:10 or 1,200 (not _NUM, which feeds float())


def check_x12(result: dict | None, chunk_text: str) -> tuple[str | None, list[str]]:
    """(summary, problems). Number guard: every number in the summary must appear in the chunk."""
    if not isinstance(result, dict):
        return None, ["no usable answer"]
    summary = _squash(str(result.get("summary") or ""))
    if not summary:
        return None, ["empty summary"]
    have = set(_SUMMARY_NUM.findall(chunk_text))
    extra = [n for n in _SUMMARY_NUM.findall(summary) if n not in have]
    if extra:
        return None, [f"the summary has numbers the chunk does not: {', '.join(extra)}"]
    return summary, []


def garbled_match(printed: str, reading: str) -> float:
    """Share of the plain (ASCII) letters of a broken-font line that a reading keeps in the same order. Broken fonts
    swap some glyphs (``PĞ.Ğstrgan`` for ``Pedestrian``) but keep the rest, so a true reading keeps most of them."""
    from difflib import SequenceMatcher
    plain = "".join(ch.lower() for ch in printed if ch.isascii() and ch.isalpha())
    read = "".join(ch.lower() for ch in reading if ch.isascii() and ch.isalpha())
    if not plain:
        return 0.0
    kept = sum(b.size for b in SequenceMatcher(None, plain, read, autojunk=False).get_matching_blocks())
    return kept / len(plain)


@dataclass
class ImageReadCheck:
    items: list[dict] = field(default_factory=list)          # {item_id, ..., ok, problems}
    distances: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    garbled: list[dict] = field(default_factory=list)        # {id, printed, reads_as, match, ok}
    texts: dict[str, dict] = field(default_factory=dict)     # description, purpose, pedestrians ... -> {text, ok, problems}
    unreadable: list[dict] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def check_x13(result: dict | None, item_ids: set[str], distance_ids: set[str], allowed: set[float],
              garbled: dict[str, str], min_match: float) -> ImageReadCheck:
    """X13 (image read): every ID must exist (distances must be distance labels), every number must be on the sheet,
    a reading of broken-font text must keep its plain letters. A failing statement is kept, marked, never edited."""
    out = ImageReadCheck()
    if not isinstance(result, dict):
        out.problems.append("no usable answer")
        return out

    def nums(text: object) -> list[str]:
        return [f"number {n:g} is not on the sheet" for n in sorted(_numbers(_SYMBOL_ID.sub(" ", str(text or ""))) - allowed)]

    for key in ("description", "drawing_type", "purpose", "pedestrians", "vehicles"):
        text = _squash(str(result.get(key) or ""))
        if text:
            p = nums(text)
            out.texts[key] = {"text": text, "ok": not p, "problems": p}
    for it in result.get("items") or []:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("item_id") or "new")
        p = [] if iid == "new" or iid in item_ids else [f"item {iid!r} does not exist"]
        p += nums(" ".join(str(it.get(k) or "") for k in ("looks_like", "meaning", "where")))
        out.items.append({**it, "item_id": iid, "ok": not p, "problems": p})
    for d in result.get("distances") or []:
        if not isinstance(d, dict):
            continue
        p = [] if str(d.get("item_id")) in distance_ids else [f"{d.get('item_id')!r} is not a distance on the sheet"]
        p += nums(d.get("measures"))
        out.distances.append({**d, "ok": not p, "problems": p})
    for r in result.get("relations") or []:
        if not isinstance(r, dict):
            continue
        p = [f"item {x!r} does not exist" for x in (r.get("a"), r.get("b")) if str(x) not in item_ids]
        out.relations.append({**r, "ok": not p, "problems": p})
    for g in result.get("garbled_text") or []:
        if not isinstance(g, dict) or str(g.get("id")) not in garbled:
            continue
        printed, reads = garbled[str(g["id"])], _squash(str(g.get("reads_as") or ""))
        m = garbled_match(printed, reads) if reads else 0.0
        out.garbled.append({"id": str(g["id"]), "printed": printed, "reads_as": reads, "match": round(m, 2),
                            "ok": bool(reads) and m >= min_match and not nums(reads)})
    out.unreadable = [u for u in result.get("unreadable") or [] if isinstance(u, dict)]
    out.inferred = [_squash(str(x)) for x in result.get("inferred") or [] if str(x).strip()]
    return out
