"""Code-side checks on model output (plan part 2). The model proposes; these decide what may be trusted.

Nothing here calls a model. A failed check never edits a value: it returns problems, and the handler stores
the value as ``unverified`` with those problems as notes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..catalog import Catalog, FieldDef, normalise
from ..numerics import extract


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


def check_x3(result: dict | None, pdf_text_in_cell: str) -> tuple[str | None, list[str]]:
    """(value, problems). The read value must agree with any real PDF text inside the cell, else unverified."""
    if not isinstance(result, dict) or result.get("readable") is not True:
        return None, ["the image was not readable"]
    value = _squash(str(result.get("value") or ""))
    if not value:
        return None, ["no value read"]
    pdf = _squash(pdf_text_in_cell)
    if pdf and pdf.lower() != value.lower() and value.lower() not in pdf.lower():
        return value, ["the read value does not match the PDF text inside the cell"]
    return value, []
