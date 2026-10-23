"""Chunk text (plan 4.3): a breadcrumb header with the page and the document context, values verbatim with every
column named, "(unconfirmed)" beside unverified values, conflicts written out with every version and its pages, and
pointers written with what they point to."""
from __future__ import annotations

import re
from typing import Any

from ..facts.model import Facts, FactValue
from ..records import Status
from .context import DocContext

UNITS = {"kmh": "km/h", "m": "m", "pct": "%", "h": "h", "km": "km"}


def pages_text(pages: list[int]) -> str:
    """[1, 2, 3, 5] -> "pages 1–3, 5"; [6] -> "page 6"."""
    ps = sorted(set(pages))
    if not ps:
        return ""
    runs, start, prev = [], ps[0], ps[0]
    for p in [*ps[1:], None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        runs.append(f"{start}" if start == prev else f"{start}–{prev}")
        if p is not None:
            start = prev = p
    return ("page " if len(ps) == 1 else "pages ") + ", ".join(runs)


def header(ctx: DocContext, sep: str, pages: list[int], path: list[str], part: str = "") -> str:
    first = min(pages) if pages else 0
    crumbs = [ctx.tmp_ref, ctx.forms.get(first) or "", *[p for p in path if p]]
    title = sep.join(c.strip() for c in crumbs if c and c.strip())
    return f"{title}{part} — {pages_text(pages)}\n{ctx.line}"


def label(fname: str) -> str:
    """posted_speed_kmh -> "Posted speed (km/h)"; first_sign_in -> "First sign in"."""
    words = fname.split("_")
    unit = UNITS.get(words[-1]) if len(words) > 1 else None
    if unit:
        words = words[:-1]
    text = " ".join(words).replace(" rp", " RP").replace("rp ", "RP ")
    text = text[:1].upper() + text[1:]
    text = re.sub(r"\b(Rp|rp)\b", "RP", text)
    for abbr in ("tmd", "tmp", "aadt", "hmv", "onrc", "tsl", "rca", "stms"):
        text = re.sub(rf"\b{abbr}\b", abbr.upper(), text, flags=re.I)
    return f"{text} ({unit})" if unit else text


def plain(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if abs(value) < 10 else f"{value:g}"
    if isinstance(value, list):
        return "; ".join(plain(v) if not isinstance(v, dict) else ", ".join(f"{k} {plain(x)}" for k, x in v.items()
                                                                          if k != "raw") for v in value)
    return str(value)


def value_text(fv: FactValue, facts: Facts | None = None) -> str:
    if fv.status == Status.CONFLICT and fv.versions:
        parts = [f"{plain(v.get('value'))} ({pages_text(v.get('pages', []))})" for v in fv.versions]
        return "conflict: " + " / ".join(parts)
    if fv.value is None and fv.link and facts is not None:
        targets = []
        for path in fv.link:
            tv = facts.value(path)
            owner = facts.fact(path.partition(".")[0])
            day = owner.fields.get("day_type") if owner else None
            if tv is not None and tv.value is not None:
                targets.append(f"{plain(day.value)} {plain(tv.value)}" if day else plain(tv.value))
        written = f"{fv.raw} " if fv.raw else ""
        return f"{written}(refers to: {'; '.join(targets)})" if targets else f"{written}(refers elsewhere)"
    shown = plain(fv.value) if fv.value is not None else (fv.raw or "not stated")
    if fv.unit and fv.value is not None and isinstance(fv.value, int | float) and fv.unit not in ("km",):
        shown = f"{shown} {fv.unit}" if not shown.endswith(fv.unit) else shown
    if fv.status == Status.UNVERIFIED:
        shown += " (unconfirmed)"
    if fv.status == Status.REVIEWED:
        shown += " (confirmed by a reviewer)"
    return shown


def field_lines(fields: dict[str, FactValue], facts: Facts, skip: tuple[str, ...] = ()) -> list[str]:
    return [f"{label(k)}: {value_text(v, facts)}" for k, v in fields.items() if k not in skip]


def glossary_matches(text: str, glossary: dict[str, str]) -> list[tuple[str, str]]:
    """The glossary abbreviations written in ``text`` as whole words, with their expansions (one rule everywhere:
    the embedding text, the keyword index and the Azure and Cosmos documents)."""
    return [(abbr, full) for abbr, full in glossary.items() if re.search(rf"\b{re.escape(abbr)}\b", text)]
