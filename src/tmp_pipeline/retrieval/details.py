"""Details pulled out of a question by code (plan 5.3, 5.4): TMP reference, times, dates relative to NZ today,
day types, TMD and RP references. Q1 gives the same details for follow-ups and hard cases; code covers the rest,
so the rules path never needs a model."""
from __future__ import annotations

import datetime as dt
import re

from ..config import RetrievalCfg

_TIME_12 = re.compile(r"(?i)\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b")
_TIME_24 = re.compile(r"(?i)(?<![\d.:/-])([01]?\d|2[0-3])([:.]?)([0-5]\d)(\s*(?:hrs?|hours|h)\b)?(?![\d./-])(?!\s*\d)")
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+(20\d{2}))?\b")
_MDY = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_WEEKDAY = re.compile(r"(?i)\b(?:(next|this|last)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
_RELATIVE = re.compile(r"(?i)\b(today|tonight|tomorrow|yesterday)\b")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TMD_REF = re.compile(r"(?i)\bTMD\s*(?:[-–]\s*)?(?:Sheet\s*|No\.?\s*)?0*(\d{1,2})\b")
RP_REF = re.compile(r"(?i)\bRP\s*(\d+(?:\.\d+)?)\b")


def tmp_refs(text: str, cfg: RetrievalCfg) -> list[str]:
    """TMP references in a canonical spelling ("kts26 - 1581" -> "KTS 26-1581")."""
    return list(dict.fromkeys(f"{m.group(1).upper()} {m.group(2)}-{m.group(3)}" for m in cfg.tmp_ref.finditer(text)))


def norm_ref(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def parse_time(text: str) -> str | None:
    """The first time of day in the text as HH:MM ("4 pm" -> "16:00", "1430 hrs" -> "14:30")."""
    m = _TIME_12.search(text)
    if m:
        h, mins = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).lower() == "pm":
            h += 12
        if h < 24 and mins < 60:
            return f"{h:02d}:{mins:02d}"
    for m in _TIME_24.finditer(text):
        hh, sep, mm, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        if sep == ":" or suffix or (len(hh) == 2 and not sep and not re.fullmatch(r"(?:19|20)\d\d", hh + mm)):
            return f"{int(hh):02d}:{mm}"
    return None


def _year_for(month: int, day: int, today: dt.date) -> int:
    """A date written without a year: the next occurrence on or after today, unless it was within the last 60 days."""
    cand = dt.date(today.year, month, day)
    return today.year + 1 if (today - cand).days > 60 else today.year


def parse_date(text: str, today: dt.date) -> dt.date | None:
    m = _ISO.search(text)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _SLASH.search(text)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))     # NZ order: day/month/year
        except ValueError:
            return None
    for rx, d_idx, m_idx in ((_DMY, 1, 2), (_MDY, 2, 1)):
        m = rx.search(text)
        if m:
            day, month = int(m.group(d_idx)), _MONTHS.index(m.group(m_idx).lower()[:3]) + 1
            try:
                year = int(m.group(3)) if m.group(3) else _year_for(month, day, today)
                return dt.date(year, month, day)
            except ValueError:
                return None
    m = _RELATIVE.search(text)
    if m:
        offset = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}[m.group(1).lower()]
        return today + dt.timedelta(days=offset)
    m = _WEEKDAY.search(text)
    if m:
        target = _DAYS.index(m.group(2).lower())
        ahead = (target - today.weekday()) % 7
        if (m.group(1) or "").lower() == "next" and ahead == 0:
            ahead = 7
        if (m.group(1) or "").lower() == "last":
            ahead = ahead - 7 if ahead else -7
        return today + dt.timedelta(days=ahead)
    return None


def day_type(text: str, cfg: RetrievalCfg) -> str | None:
    return next((k for k, rx in cfg.day_types.items() if rx.search(text)), None)


def rp_values(text: str) -> list[float]:
    return list(dict.fromkeys(float(m.group(1)) for m in RP_REF.finditer(text)))


def code_details(question: str, cfg: RetrievalCfg, today: dt.date) -> dict[str, str]:
    """The details code can read reliably, in the shape Q1 returns."""
    out: dict[str, str] = {}
    refs = tmp_refs(question, cfg)
    if refs:
        out["tmp_ref"] = refs[0]
    t = parse_time(question)
    if t:
        out["time"] = t
    d = parse_date(question, today)
    if d:
        out["date_iso"] = d.isoformat()
    dtp = day_type(question, cfg)
    if dtp:
        out["day_type"] = dtp
    tmd = TMD_REF.search(question)
    if tmd:
        out["tmd"] = tmd.group(0)
    rps = rp_values(question)
    if rps:
        out["rp"] = f"{rps[0]:g}"
    return out
