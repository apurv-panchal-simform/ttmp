"""Numerics and identifiers (plan section 2.5): typed extraction from text, each with its position.

Every hit keeps the raw text exactly as written next to the clean typed value, and its character offsets, so
a fact can always quote its source. Extraction is plain code; nothing here calls a model. When two patterns
claim the same characters the earlier kind in ``PRIORITY`` wins (a date beats the time-like digits inside it).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))

PRIORITY = ("date", "time", "rp", "speed", "percent", "flow", "distance", "weight", "duration", "tmd", "tmp_reference",
            "phone")


@dataclass(frozen=True)
class Numeric:
    kind: str            # one of PRIORITY
    raw: str             # exactly as written
    value: float | int | str
    unit: str | None
    start: int           # character offsets into the text it came from
    end: int


def _num(s: str) -> float | int:
    f = float(s)
    return int(f) if f.is_integer() and "." not in s else f


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _date_words(m: re.Match) -> Numeric | None:
    iso = _iso(int(m["y"]), MONTHS[m["mon"].lower()], int(m["d"]))
    return Numeric("date", m.group(0), iso, None, m.start(), m.end()) if iso else None


def _date_slash(m: re.Match) -> Numeric | None:        # NZ order: day/month/year
    y = int(m["y"]) + (2000 if len(m["y"]) == 2 else 0)
    iso = _iso(y, int(m["m"]), int(m["d"]))
    return Numeric("date", m.group(0), iso, None, m.start(), m.end()) if iso else None


def _time_hrs(m: re.Match) -> Numeric | None:
    hh, mm = int(m["h"]), int(m["mi"])
    if hh > 23 or mm > 59:
        return None
    return Numeric("time", m.group(0), f"{hh:02d}:{mm:02d}", None, m.start(), m.end())


def _time_ampm(m: re.Match) -> Numeric | None:
    hh, mm = int(m["h"]), int(m["mi"] or 0)
    if not 1 <= hh <= 12 or mm > 59:
        return None
    hh = hh % 12 + (12 if m["ap"].lower() == "pm" else 0)
    return Numeric("time", m.group(0), f"{hh:02d}:{mm:02d}", None, m.start(), m.end())


_DURATION_UNITS = {"shift": "shift", "shifts": "shift", "day": "day", "days": "day", "hour": "h", "hours": "h",
                   "hr": "h", "hrs": "h", "minute": "min", "minutes": "min", "min": "min", "mins": "min",
                   "week": "week", "weeks": "week", "night": "night", "nights": "night"}
_DIST_UNITS = {"m": "m", "metres": "m", "meters": "m", "metre": "m", "meter": "m", "km": "km"}

_RULES: list[tuple[str, re.Pattern[str], Callable[[re.Match], Numeric | None]]] = [
    ("date", re.compile(rf"(?<!\d )(?<!\d)\b(?P<d>\d{{1,2}})\s*(?P<mon>{_MONTH_RX})\s+(?P<y>\d{{4}})\b", re.I), _date_words),
    ("date", re.compile(r"\b(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4}|\d{2})\b"), _date_slash),
    ("time", re.compile(r"\b(?P<h>[01]\d|2[0-3])(?P<mi>[0-5]\d)\s*(?:hrs?|hours)\b", re.I), _time_hrs),
    ("time", re.compile(r"\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>am|pm)\b", re.I), _time_ampm),
    ("rp", re.compile(r"\bRP\s*(?P<v>\d+(?:\.\d+)?)\b", re.I),
     lambda m: Numeric("rp", m.group(0), float(m["v"]), "km", m.start(), m.end())),
    ("speed", re.compile(r"(?<![\w.])(?P<v>\d{2,3})\s*km\s*/?\s*h(?:r)?\b", re.I),
     lambda m: Numeric("speed", m.group(0), int(m["v"]), "km/h", m.start(), m.end())),
    ("percent", re.compile(r"(?<![\w.])(?P<v>\d+(?:\.\d+)?)\s*%"),
     lambda m: Numeric("percent", m.group(0), float(m["v"]), "%", m.start(), m.end())),
    ("flow", re.compile(r"(?<![\w.])(?P<v>\d+)\s*vph\b", re.I),
     lambda m: Numeric("flow", m.group(0), int(m["v"]), "vph", m.start(), m.end())),
    ("distance", re.compile(r"(?<![\w.])(?P<v>\d+(?:\.\d+)?)\s*(?P<u>km|metres?|meters?|m)(?![A-Za-z])", re.I),
     lambda m: Numeric("distance", m.group(0), _num(m["v"]), _DIST_UNITS[m["u"].lower()], m.start(), m.end())),
    ("weight", re.compile(r"(?<![\w.])(?P<v>\d+(?:\.\d+)?)\s*(?P<u>tonnes?|kg|t)(?![A-Za-z])", re.I),
     lambda m: Numeric("weight", m.group(0), _num(m["v"]), "kg" if m["u"].lower() == "kg" else "t",
                       m.start(), m.end())),
    ("duration", re.compile(
        r"(?:\bevery\s+)?(?<![\w.])(?P<v>\d+(?:\.\d+)?)\s*(?P<u>shifts?|days?|hours?|hrs?|minutes?|mins?|weeks?|nights?)\b",
        re.I),
     lambda m: Numeric("duration", m.group(0), _num(m["v"]), _DURATION_UNITS[m["u"].lower()], m.start(), m.end())),
    ("tmd", re.compile(r"\bTMD\s*(?:Sheet\s*|No\.?\s*)?0*(?P<n>\d{1,2})\b", re.I),
     lambda m: Numeric("tmd", m.group(0), f"TMD-{int(m['n']):02d}", None, m.start(), m.end())),
    ("tmp_reference", re.compile(r"\b[A-Z]{2,5}\s?\d{2}-\d{4}\b"),
     lambda m: Numeric("tmp_reference", m.group(0), re.sub(r"^([A-Z]+)\s*", r"\1 ", m.group(0)), None, m.start(),
                       m.end())),
    ("phone", re.compile(r"(?<![\w])(?:0800\s?[A-Z]{6}\b|0800\s?\d{3}\s?\d{3,4}\b|\+64\s?\d{1,2}\s?\d{3}\s?\d{3,4}\b"
                         r"|0[2-9]\d?\s?\d{3}\s?\d{4}\b)"),
     lambda m: Numeric("phone", m.group(0), re.sub(r"\s+", " ", m.group(0)), None, m.start(), m.end())),
]


def extract(text: str, kinds: tuple[str, ...] | None = None) -> list[Numeric]:
    """All typed items in ``text`` in reading order. Overlaps go to the kind earlier in PRIORITY."""
    found: list[Numeric] = []
    for _kind, rx, make in sorted(_RULES, key=lambda r: PRIORITY.index(r[0])):
        if kinds is not None and _kind not in kinds:
            continue
        for m in rx.finditer(text):
            n = make(m)
            if n is None or any(n.start < f.end and f.start < n.end for f in found):
                continue
            found.append(n)
    return sorted(found, key=lambda n: n.start)


# --- single cell values -------------------------------------------------------------------------------------------

_PLAIN_NUMBER = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")


def parse_value(text: str, ftype: str, unit: str | None = None) -> tuple[object | None, str | None]:
    """Clean typed value for a whole cell, as (value, problem). The raw text is always kept by the caller.

    ftype is a catalog field type: text, integer, number, percent, date, time, rp, speed, distance, duration,
    bool. A cell that does not hold the expected kind gives (None, "<reason>"): the caller marks it unverified
    rather than guessing."""
    t = " ".join(text.split())
    if ftype == "text":
        return (t or None), None if t else "empty"
    if not t:
        return None, "empty"
    kind = {"rp": "rp", "speed": "speed", "percent": "percent", "date": "date", "time": "time",
            "distance": "distance", "duration": "duration"}.get(ftype)
    if kind is not None:
        hits = [n for n in extract(t, (kind,)) if n.start == 0 and n.end == len(t)]
        if hits:
            return hits[0].value, None
        if ftype == "percent" and _PLAIN_NUMBER.match(t):
            return float(t), None
        if ftype == "distance" and _PLAIN_NUMBER.match(t):
            return _num(t), None
        return None, f"not a {ftype}"
    if ftype in ("integer", "number"):
        stripped = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
        if unit:                                              # accept the unit written after the number
            stripped = re.sub(rf"\s*{re.escape(unit)}\s*$", "", stripped, flags=re.I)
        if _PLAIN_NUMBER.match(stripped):
            v = _num(stripped.strip())
            if ftype == "integer" and isinstance(v, float):
                return None, "expected a whole number"
            return v, None
        rp = [n for n in extract(t, ("rp",)) if n.start == 0 and n.end == len(t)]
        if rp and ftype == "number":
            return rp[0].value, None
        return None, f"not a {ftype}"
    return None, f"unsupported type {ftype}"
