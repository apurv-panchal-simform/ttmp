"""``time_check`` (plan 5.8): is work, the temporary speed limit or a stage allowed / in force at a NZ date and time?

Uses the typed shift windows, the TSL hours by day type, the works dates and each stage's hours. A date becomes a
day type through the school-term and public-holiday calendar (``data/nz_calendar.toml``); without the calendar, or
on a day the calendar can't settle, every day type's window is returned with "school day or holiday not known".
"""
from __future__ import annotations

import datetime as dt
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

from ..config import Config
from ..facts.common import day_type as facts_day_type
from ..facts.model import Fact, Facts
from ..records import Status

# catalog fields the windows are read from
WORK_START, WORK_END, PICK_UP = "first_sign_in", "site_cleared", "pick_up"
_ALWAYS = re.compile(r"(?i)24\s*[x×/]\s*7|24\s*hours?|all day|at all times")
_DAYSHIFT = re.compile(r"(?i)day\s*shift")
_LABEL = {"school_day": "school days", "school_holiday": "school holidays", "weekend": "weekends",
          "public_holiday": "public holidays"}


@dataclass(frozen=True)
class Calendar:
    covers: tuple[dt.date, dt.date]
    terms: tuple[tuple[dt.date, dt.date], ...]
    uncertain: tuple[tuple[dt.date, dt.date], ...]
    holidays: dict[dt.date, str] = field(default_factory=dict)

    def day_type(self, day: dt.date) -> tuple[str | None, str]:
        """(day type or None, how it was decided). Weekends need no calendar; the rest do."""
        name = self.holidays.get(day)
        if name:
            return "public_holiday", name
        if day.weekday() >= 5:
            return "weekend", f"{day:%A}"
        if not (self.covers[0] <= day <= self.covers[1]):
            return None, "the school-term calendar does not cover this date"
        if any(a <= day <= b for a, b in self.uncertain):
            return None, "schools set their own term dates around this day"
        if any(a <= day <= b for a, b in self.terms):
            return "school_day", "from the school-term calendar"
        return "school_holiday", "from the school-term calendar"


@cache
def load_calendar(name: str) -> Calendar | None:
    if not name:
        return None
    raw = tomllib.loads(resources.files("tmp_pipeline").joinpath("data", name).read_text(encoding="utf-8"))
    d = dt.date.fromisoformat
    return Calendar((d(raw["covers"][0]), d(raw["covers"][1])),
                    tuple((d(a), d(b)) for a, b in raw.get("terms", [])),
                    tuple((d(a), d(b)) for a, b in raw.get("uncertain", [])),
                    {d(day): name for day, name in raw.get("public_holidays", [])})


def no_calendar_day_type(day: dt.date) -> tuple[str | None, str]:
    return ("weekend", f"{day:%A}") if day.weekday() >= 5 else (None, "school day or holiday not known (no calendar loaded)")


def _hhmm(value: object) -> dt.time | None:
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})", str(value or "").strip())
    return dt.time(int(m.group(1)), int(m.group(2))) if m and int(m.group(1)) < 24 else None


def _usable(fact: Fact, name: str) -> object:
    fv = fact.fields.get(name)
    return fv.value if fv is not None and fv.status != Status.CONFLICT else None


@dataclass
class Window:
    day_type: str                       # question day type (school_day ...)
    start: dt.time
    end: dt.time
    text: str
    source: str
    pick_up: dt.time | None = None


def _shift_windows(facts: Facts, cfg: Config, cite: Callable[[Fact], str]) -> dict[str, Window]:
    to_q = {v: k for k, v in cfg.retrieval.day_type_facts.items()}
    out: dict[str, Window] = {}
    for f in facts.of("shift_window"):
        dtp = facts_day_type(str(_usable(f, "day_type") or ""), cfg.facts)
        q = to_q.get(dtp or "")
        start, end = _hhmm(_usable(f, WORK_START)), _hhmm(_usable(f, WORK_END))
        if q and start and end:
            pick = _hhmm(_usable(f, PICK_UP))
            text = (f"{_LABEL[q]}: first sign-in {start:%H:%M}" + (f", pick-up from {pick:%H:%M}" if pick else "")
                    + f", site cleared {end:%H:%M}")
            out[q] = Window(q, start, end, text, cite(f), pick)
    return out


def _tsl_windows(facts: Facts, cfg: Config, cite: Callable[[Fact], str]) -> dict[str, Window]:
    to_q = {v: k for k, v in cfg.retrieval.day_type_facts.items()}
    out: dict[str, Window] = {}
    for f in facts.of("speed_limit"):
        speed = _usable(f, "speed_kmh")
        rows = _usable(f, "hours_by_day_type")
        for row in rows if isinstance(rows, list) else []:
            q = to_q.get(str(row.get("day_type") or ""))
            start, end = _hhmm(row.get("from")), _hhmm(row.get("to"))
            if q and start and end:
                out[q] = Window(q, start, end, f"{_LABEL[q]}: {speed} km/h TSL in force {start:%H:%M}–{end:%H:%M}",
                                cite(f))
    return out


def _dates(facts: Facts, entity: str, a: str, b: str) -> tuple[dt.date | None, dt.date | None, Fact | None]:
    for f in facts.of(entity):
        s, e = _usable(f, a), _usable(f, b)
        try:
            return (dt.date.fromisoformat(str(s)) if s else None), (dt.date.fromisoformat(str(e)) if e else None), f
        except ValueError:
            continue
    return None, None, None


def _judge(w: Window, t: dt.time | None, what: str) -> tuple[bool | None, str]:
    if t is None:
        return None, w.text
    if t < w.start:
        return False, f"{t:%H:%M} is before the {w.start:%H:%M} " + ("start" if what == "tsl" else "first sign-in time")
    if t >= w.end:
        return False, f"{t:%H:%M} is after the {w.end:%H:%M} " + ("end of the TSL hours" if what == "tsl"
                                                                     else "site-cleared time")
    note = f" (pick-up starts {w.pick_up:%H:%M})" if w.pick_up and t >= w.pick_up else ""
    return True, f"{t:%H:%M} is within {w.start:%H:%M}–{w.end:%H:%M}{note}"


def time_check(facts: Facts, cfg: Config, cite: Callable[[Fact], str], date_iso: str | None = None,
               time: str | None = None, day_type: str | None = None, what: str = "work",
               calendar: Calendar | None = None) -> dict:
    """{"allowed": True|False|None, "day_type", "window", "reason", "sources", "per_day_type"?, "stages"?}."""
    t = _hhmm(time) if time else None
    day = None
    if date_iso:
        try:
            day = dt.date.fromisoformat(date_iso)
        except ValueError:
            return {"allowed": None, "reason": f"{date_iso!r} is not a date", "sources": []}
    how = "as asked" if day_type else ""
    if day_type is None and day is not None:
        day_type, how = calendar.day_type(day) if calendar else no_calendar_day_type(day)
    out: dict = {"what": what, "date": day.isoformat() if day else None, "time": f"{t:%H:%M}" if t else None,
                 "day_type": f"{day_type} ({how})" if day_type else how or "not given", "sources": []}

    start, end, period = _dates(facts, "speed_limit" if what == "tsl" else "work_period",
                                "date_from" if what == "tsl" else "start_date", "date_to" if what == "tsl" else "end_date")
    if day is not None and start and end and not (start <= day <= end):
        out.update(allowed=False, reason=f"{day.isoformat()} is outside the {'TSL' if what == 'tsl' else 'works'} dates "
                                          f"{start.isoformat()} to {end.isoformat()}", window=None)
        out["sources"].append(cite(period) if period else "")
        return out

    if what == "stage":
        shifts = _shift_windows(facts, cfg, cite)
        rows = []
        for f in facts.of("stage"):
            hours = str(_usable(f, "hours") or "")
            name = f"{_usable(f, 'tmd_id') or ''} {_usable(f, 'title') or ''}".strip()
            if _ALWAYS.search(hours):
                rows.append({"stage": name, "allowed": True, "reason": f"{hours} (all day)", "source": cite(f)})
            elif _DAYSHIFT.search(hours) and day_type in shifts:
                ok, why = _judge(shifts[day_type], t, "work")
                rows.append({"stage": name, "allowed": ok, "reason": f"{hours}: {why}", "source": cite(f)})
            else:
                rows.append({"stage": name, "allowed": None, "reason": f"hours: {hours or 'not stated'}",
                             "source": cite(f)})
        out["stages"] = rows
        out["sources"] = list(dict.fromkeys(r["source"] for r in rows))
        return out

    windows = _tsl_windows(facts, cfg, cite) if what == "tsl" else _shift_windows(facts, cfg, cite)
    if day_type in windows:
        w = windows[day_type]
        ok, why = _judge(w, t, what)
        out.update(allowed=ok, window=w.text, reason=why)
        out["sources"].append(w.source)
    elif day_type:
        out.update(allowed=None, window=None,
                   reason=f"the TMP gives no {'TSL ' if what == 'tsl' else ''}hours for {_LABEL.get(day_type, day_type)}")
    else:
        per = []
        for q, w in windows.items():
            ok, why = _judge(w, t, what)
            per.append({"day_type": q, "allowed": ok, "window": w.text, "reason": why})
        out.update(allowed=None, window=None, per_day_type=per,
                   reason=f"{how or 'the day type is not known'}; every window is listed")
        out["sources"] = list(dict.fromkeys(w.source for w in windows.values()))
    return out
