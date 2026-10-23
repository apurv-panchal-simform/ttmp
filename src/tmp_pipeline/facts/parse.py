"""Facts the handlers leave inside text (plan section 3.2): TMP reference numbers, the client order reference,
temporary speed limit rows, the stages of the works and named contacts.

Everything is read with ``numerics.extract`` and the cues in ``[facts]``; a value that cannot be read stays
unverified with the reason. Nothing is guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import FactsCfg
from ..models import ElementStore
from ..numerics import Numeric, extract
from ..records import RecordStore, Status
from .common import day_type, norm_name, record_texts, source_of
from .model import FactValue, Source, TmdMention


@dataclass
class Occurrence:
    value: str
    source: Source
    method: str                     # furniture | text | vector


def tmp_references(store: ElementStore, records: RecordStore) -> list[Occurrence]:
    """Every place the document states its TMP reference: page headers, any record's text, sheet title blocks."""
    out: list[Occurrence] = []
    seen: set[tuple[str, int, str]] = set()

    def add(value: str, src: Source, method: str) -> None:
        key = (value, src.page, src.record_id)
        if key not in seen:
            seen.add(key)
            out.append(Occurrence(value, src, method))

    for page in sorted(store.pages):
        ref = store.pages[page].tmp_reference
        if ref:
            add(ref, Source("", page, [], ref), "furniture")
    for rec in records.records.values():
        if rec.kind == "diagram_sheet":
            fv = rec.fields.get("tmp_number")
            if fv is not None and fv.value:
                for n in extract(str(fv.raw or fv.value), ("tmp_reference",)):
                    add(str(n.value), source_of(rec, str(fv.raw or fv.value)), "vector")
            continue
        for text, _ in record_texts(rec):
            for n in extract(text, ("tmp_reference",)):
                add(str(n.value), source_of(rec, n.raw), "text")
    return out


def order_reference(records: RecordStore, cfg: FactsCfg) -> FactValue | None:
    """The client order reference from the "TMP Order / Reference" pair, without the TMP reference beside it."""
    for rec in records.of_kind("table"):
        for p in rec.data.get("pairs") or []:
            label, value = str(p.get("label") or ""), str(p.get("value") or "")
            if not cfg.order_reference_label.search(label) or not value:
                continue
            rest = value
            for n in extract(value, ("tmp_reference",)):
                rest = rest.replace(n.raw, " ")
            rest = re.sub(r"\s*[/|,;]\s*$|^\s*[/|,;]\s*", "", rest.strip()).strip(" /")
            if rest:
                return FactValue(rest, value, sources=[source_of(rec, value)], method="pattern")
    return None


@dataclass
class TslRow:
    source: Source
    record_id: str
    fields: dict[str, FactValue] = field(default_factory=dict)


def _first(nums: list[Numeric], kind: str) -> Numeric | None:
    return next((n for n in nums if n.kind == kind), None)


def tsl_rows(records: RecordStore, roads: list[str], cfg: FactsCfg) -> list[TslRow]:
    """Temporary speed limit rows: a table row whose headers mention the limit and whose text states a speed.
    ``roads`` are the road names already known, used to say which road the limit is on."""
    out: list[TslRow] = []
    for rec in records.of_kind("table_row"):
        headers = " ".join(str(h) for h in rec.data.get("headers") or [])
        if not cfg.tsl_row.search(headers):
            continue
        text = " ".join(t for t, _ in record_texts(rec))
        nums = extract(text)
        speed = _first(nums, "speed")
        if speed is None:
            continue                                     # "Not Required for this activity"
        src = source_of(rec, text)
        f: dict[str, FactValue] = {"speed_kmh": FactValue(speed.value, speed.raw, "km/h", sources=[src],
                                                          method="pattern")}
        dist = next((n for n in nums if n.kind == "distance" and n.unit == "m"), None)
        if dist is not None:
            f["length_m"] = FactValue(dist.value, dist.raw, "m", sources=[src], method="pattern")
        rps = [n for n in nums if n.kind == "rp"]
        if len(rps) >= 2:
            lo, hi = sorted(rps[:2], key=lambda n: float(n.value))
            f["rp_from"] = FactValue(lo.value, lo.raw, "km", sources=[src], method="pattern")
            f["rp_to"] = FactValue(hi.value, hi.raw, "km", sources=[src], method="pattern")
        else:
            f["rp_from"] = FactValue(None, None, "km", Status.UNVERIFIED, "pattern", [src],
                                     ["the row does not state two route positions"])
        said = f" {norm_name(text, cfg)} "
        road = next((r for r in roads if f" {norm_name(r, cfg)} " in said), None)
        if road:
            f["road_name"] = FactValue(road, road, sources=[src], method="pattern")
        hours = _hours(text, nums, cfg)
        if hours:
            f["hours_by_day_type"] = FactValue(hours, None, sources=[src], method="pattern")
        dates = [n for n in nums if n.kind == "date"]
        if dates:
            f["date_from"] = FactValue(dates[0].value, dates[0].raw, sources=[src], method="pattern")
            f["date_to"] = FactValue(dates[-1].value, dates[-1].raw, sources=[src], method="pattern")
        out.append(TslRow(src, rec.record_id, f))
    return out


def _hours(text: str, nums: list[Numeric], cfg: FactsCfg) -> list[dict]:
    """Time ranges in reading order, each with the day type written after it ("9am - 3pm (during school days)")."""
    times = [n for n in nums if n.kind == "time"]
    out = []
    for a, b in zip(times[0::2], times[1::2], strict=False):
        nxt = times[times.index(b) + 1].start if times.index(b) + 1 < len(times) else len(text)
        out.append({"day_type": day_type(text[b.end:nxt], cfg), "from": a.value, "to": b.value,
                    "raw": text[a.start:b.end]})
    return out


@dataclass
class StageEntry:
    tmd_id: str
    title: str
    hours: str | None
    source: Source


def stage_entries(lists: dict[tuple[int, str], list[TmdMention]], records: RecordStore) -> list[StageEntry]:
    """One entry per mapped mention in a stage list. A stage given as a heading takes its hours from the text under
    it ("24x7 hrs", "(Dayshift)")."""
    out = []
    for key in sorted(lists):
        for m in lists[key]:
            if not m.mapped_to:
                continue
            rec = records.records.get(m.source.record_id)
            hours = None
            if rec is not None and rec.kind == "section":
                hours = str(rec.data.get("text") or "").strip().strip("()").strip() or None
            out.append(StageEntry(m.mapped_to, m.title or m.text, hours, m.source))
    return out


_NAME_DIGITS = re.compile(r"^(.*?\S)\s*[-–]\s*(\+?[\d ]{7,})$")


def contacts_from_pairs(records: RecordStore, cfg: FactsCfg) -> list[tuple[dict[str, str], Source]]:
    """Contacts given as a label/value pair ("Client (Contracting PCBU)": "James Lu - 021 733 454")."""
    out = []
    for rec in records.of_kind("table"):
        for p in rec.data.get("pairs") or []:
            label, value = str(p.get("label") or "").strip(), str(p.get("value") or "").strip()
            if not cfg.contact_labels.search(label) or not value:
                continue
            lines = [str(x).strip() for x in p.get("value_lines") or [] if str(x).strip()]
            organisation = None
            if len(lines) > 1:            # "Watercare Services (Water)" over "James Lu - 021 733 454": org, then person
                person = next((x for x in reversed(lines) if re.search(r"\d", x)), lines[-1])
                organisation = " ".join(x for x in lines if x is not person)
                organisation = re.sub(r"\s*\([^)]*\)\s*$", "", organisation).strip() or None   # "(Water)": the utility
                value = person
            typed = extract(value, ("phone",))
            phones = [str(n.value) for n in typed]
            name = value
            for n in typed:
                name = name.replace(n.raw, " ")
            tail = _NAME_DIGITS.match(" ".join(name.split()))
            if not phones and tail:                      # "James Lu - 021 733 454": a number numerics does not type
                name, phones = tail.group(1), [" ".join(tail.group(2).split())]
            name = re.sub(r"\s*[-–,]\s*$", "", " ".join(name.split())).strip()
            if not name or name.lower() in ("n/a", "na", "none"):
                continue
            item = {"name": name, "role": label}
            if phones:
                item["phone"] = phones[0]
            if organisation:
                item["organisation"] = organisation
            out.append((item, source_of(rec, value)))
    return out
