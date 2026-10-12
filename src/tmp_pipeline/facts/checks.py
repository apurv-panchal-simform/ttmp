"""Checks (plan section 3.3). Code only; a check never blocks and never corrects. A contradiction becomes a
``Conflict`` with every version kept and the values it touches marked ``conflict``; rating problems are flagged.

Conflict ids are given in rule order, so the same document always gets the same ids.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config import FactsCfg
from ..models import ElementStore
from ..records import RecordStore, Status
from ..vocab import Flag, Outcome
from .common import day_type, norm_name, record_texts
from .mentions import stage_lists
from .model import CheckResult, Conflict, Facts


@dataclass
class Ctx:
    facts: Facts
    store: ElementStore
    records: RecordStore
    cfg: FactsCfg


Found = tuple[CheckResult, list[Conflict]]


def _conflict(rule: int, title: str, versions: list[dict], affected: list[str]) -> Conflict:
    return Conflict("", rule, title, versions, affected)


def rule_tmp_reference(c: Ctx) -> Found:
    doc = next(iter(c.facts.of("tmp_document")), None)
    fv = doc.fields.get("tmp_reference") if doc else None
    if fv is None:
        return CheckResult(1, "tmp_reference", "not_checked", "no TMP reference found"), []
    if fv.status != Status.CONFLICT:
        return CheckResult(1, "tmp_reference", "pass", f"{fv.value} everywhere"), []
    path = f"{doc.fact_id}.tmp_reference"         # type: ignore[union-attr]
    return (CheckResult(1, "tmp_reference", "conflict", " vs ".join(str(v["value"]) for v in fv.versions)),
            [_conflict(1, "TMP reference differs", fv.versions, [path])])


def rule_tmd_numbers(c: Ctx) -> Found:
    bad = [m for m in c.facts.mentions if m.id_conflict]
    if not bad:
        return CheckResult(2, "tmd_number_title", "pass", "every mention's number agrees with its title"), []
    versions: dict[str, dict] = {}
    for m in bad:
        m.status = Status.CONFLICT
        v = versions.setdefault(" ".join(m.text.split()).lower(), {
            "value": m.text, "pages": [], "sources": [], "mapped_to": m.mapped_to,
            "note": m.notes[-1] if m.notes else ""})
        v["pages"] = sorted({*v["pages"], m.page})
        v["sources"].append(m.mention_id)
    pages = sorted({m.page for m in bad})
    return (CheckResult(2, "tmd_number_title", "conflict", f"{len(bad)} mentions on pages {pages}"),
            [_conflict(2, "Diagram number disagrees with its title", list(versions.values()),
                       [m.mention_id for m in bad])])


def rule_stage_lists(c: Ctx) -> Found:
    sheets = [s.tmd_id for s in c.facts.tmd_sheets]
    lists = stage_lists(c.facts.mentions, c.cfg)
    if not lists or not sheets:
        return CheckResult(3, "stage_lists", "not_checked", "no stage list or no diagram sheets"), []
    missing: dict[tuple[str, ...], list[int]] = {}
    for (page, _), ms in sorted(lists.items()):
        gone = tuple(t for t in sheets if t not in {m.mapped_to for m in ms})
        if gone:
            missing.setdefault(gone, []).append(page)
    if not missing:
        return CheckResult(3, "stage_lists", "pass", "every diagram appears in each stage list"), []
    titles = {s.tmd_id: s.title for s in c.facts.tmd_sheets}
    out = []
    for gone, pages in missing.items():
        names = ", ".join(f"{t} ({titles.get(t, '')})" for t in gone)
        out.append(_conflict(3, "A diagram is missing from a stage list", [
            {"value": f"stage list without {names}", "pages": pages},
            {"value": f"diagram sheets {', '.join(sheets)}", "pages": [s.page for s in c.facts.tmd_sheets]}],
            [f"stage_list:p{p}" for p in pages] + list(gone)))
    return CheckResult(3, "stage_lists", "conflict", "; ".join(o.versions[0]["value"] for o in out)), out


def rule_traffic_flow(c: Ctx) -> Found:
    modes = {mode: [s for s in c.facts.tmd_sheets if rx.search(s.title)] for mode, rx in c.cfg.diagram_modes.items()}
    out = []
    for rec in sorted(c.records.records.values(), key=lambda r: (r.page, r.record_id)):
        for text, _ in record_texts(rec):
            for claim, rx in c.cfg.flow_statements.items():
                m = rx.search(text)
                if not m:
                    continue
                for pair in c.cfg.incompatible_modes:
                    a, _, b = pair.partition(":")
                    if a != claim:
                        continue
                    for sheet in modes.get(b, []):
                        sentence = next((s.strip() for s in text.split(".") if m.group(0) in s), m.group(0))
                        out.append(_conflict(4, "A traffic-flow statement contradicts a diagram", [
                            {"value": sentence, "pages": [rec.page], "sources": [rec.record_id]},
                            {"value": f"{sheet.tmd_id} {sheet.title}", "pages": [sheet.page],
                             "sources": [sheet.record_id]}], [rec.record_id, sheet.tmd_id]))
    if not out:
        return CheckResult(4, "traffic_flow", "pass", "no statement contradicts a diagram"), []
    return CheckResult(4, "traffic_flow", "conflict", f"{len(out)} statement(s)"), out


def _road(c: Ctx, name: str | None):
    if not name:
        return None
    return next((r for r in c.facts.of("road") if "road_name" in r.fields
                 and norm_name(str(r.fields["road_name"].value or ""), c.cfg) == norm_name(name, c.cfg)), None)


def rule_tsl_extent(c: Ctx) -> Found:
    limits = c.facts.of("speed_limit")
    if not limits:
        return CheckResult(5, "tsl_extent", "not_checked", "no temporary speed limit"), []
    out, notes = [], []
    for sl in limits:
        f = sl.fields
        road = _road(c, f["road_name"].value if "road_name" in f else None)
        if road is None or not all(k in f and f[k].value is not None for k in ("rp_from", "rp_to")):
            notes.append(f"{sl.fact_id}: road or route positions unknown")
            continue
        lo, hi = road.fields.get("start_rp"), road.fields.get("end_rp")
        if lo is None or hi is None or lo.value is None or hi.value is None:
            notes.append(f"{sl.fact_id}: the road's extent is unknown")
            continue
        a, b = float(f["rp_from"].value), float(f["rp_to"].value)
        if float(lo.value) <= a <= b <= float(hi.value):
            notes.append(f"RP {a:.3f}-{b:.3f} within {float(lo.value):.3f}-{float(hi.value):.3f}")
            continue
        out.append(_conflict(5, "Speed-limit extent outside the works extent", [
            {"value": f"RP {a:.3f}-{b:.3f}", "pages": sl.pages()},
            {"value": f"RP {float(lo.value):.3f}-{float(hi.value):.3f}", "pages": road.pages()}],
            [f"{sl.fact_id}.rp_from", f"{sl.fact_id}.rp_to"]))
    outcome = "conflict" if out else ("pass" if any("within" in n for n in notes) else "not_checked")
    return CheckResult(5, "tsl_extent", outcome, "; ".join(notes)), out


def rule_tsl_hours(c: Ctx) -> Found:
    windows = {}
    for w in c.facts.of("shift_window"):
        dt = day_type(str(w.fields["day_type"].value), c.cfg) if "day_type" in w.fields else None
        if dt:
            windows[dt] = w
    out, checked = [], 0
    for sl in c.facts.of("speed_limit"):
        hours = sl.fields.get("hours_by_day_type")
        for h in (hours.value if hours else None) or []:
            win = windows.get(h.get("day_type"))
            if win is None:
                continue
            start, end = win.fields.get("first_sign_in"), win.fields.get("site_cleared")
            if start is None or end is None:
                continue
            checked += 1
            if (start.value, end.value) != (h["from"], h["to"]):
                out.append(_conflict(6, "Speed-limit hours differ from the shift window", [
                    {"value": f"{h['from']}-{h['to']} ({h['day_type']})", "pages": sl.pages()},
                    {"value": f"{start.value}-{end.value}", "pages": win.pages()}],
                    [f"{sl.fact_id}.hours_by_day_type", f"{win.fact_id}.first_sign_in"]))
    if out:
        return CheckResult(6, "tsl_hours", "conflict", f"{len(out)} day type(s) differ"), out
    return CheckResult(6, "tsl_hours", "pass" if checked else "not_checked", f"{checked} day type(s) agree"), []


def _date_values(c: Ctx, names: tuple[str, ...]) -> list[dict]:
    vals: list[dict] = []
    for name in names:
        entity, _, fname = name.partition(".")
        for f in c.facts.of(entity):
            fv = f.fields.get(fname)
            if fv is None:
                continue
            for v in (fv.versions or [{"value": fv.value, "pages": f.pages()}]):
                if v["value"] is not None:
                    vals.append({"value": v["value"], "pages": v["pages"], "path": f"{f.fact_id}.{fname}"})
    return vals


def rule_dates(c: Ctx) -> Found:
    out, seen = [], 0
    for which, names in (("start", c.cfg.start_date_fields), ("end", c.cfg.end_date_fields)):
        vals = _date_values(c, names)
        seen += len(vals)
        distinct = sorted({str(v["value"]) for v in vals})
        if len(distinct) > 1:
            versions = [{"value": d, "pages": sorted({p for v in vals if str(v["value"]) == d for p in v["pages"]})}
                        for d in distinct]
            out.append(_conflict(7, f"Work {which} dates differ", versions, sorted({v["path"] for v in vals})))
    if out:
        return CheckResult(7, "dates", "conflict", f"{len(out)} date(s) differ"), out
    return CheckResult(7, "dates", "pass" if seen else "not_checked", "start and end dates agree"), []


def rule_merged(c: Ctx) -> Found:
    out = []
    for f in c.facts.facts:
        for fname, fv in f.fields.items():
            if fv.status == Status.CONFLICT and fv.method == "merge":
                out.append(_conflict(8, f"{f.entity} {fname} differs between records", fv.versions,
                                     [f"{f.fact_id}.{fname}"]))
    if out:
        return CheckResult(8, "merged_values", "conflict", f"{len(out)} value(s) clash"), out
    return CheckResult(8, "merged_values", "pass", "merged records have no clashing values"), []


def rule_ratings(c: Ctx) -> Found:
    bad = [f.fact_id for f in c.facts.of("risk") if str(Flag.RATING_MISMATCH) in f.flags]
    n = len(c.facts.of("risk"))
    if not n:
        return CheckResult(9, "risk_rating", "not_checked", "no risk register"), []
    return (CheckResult(9, "risk_rating", "flagged" if bad else "pass",
                        f"{len(bad)} of {n} rows disagree with the matrix", items=bad), [])


def rule_cell_colours(c: Ctx) -> Found:
    cells = c.facts.of("risk_matrix_cell")
    if not cells:
        return CheckResult(10, "cell_colour", "not_checked", "no risk matrix"), []
    bad = [f.fact_id for f in cells if "rating" not in f.fields or f.fields["rating"].value is None
           or f.fields["rating"].status != Status.VERIFIED]
    return (CheckResult(10, "cell_colour", "flagged" if bad else "pass",
                        f"{len(bad)} of {len(cells)} cells have a colour no key rating explains", items=bad), [])


RULES: list[Callable[[Ctx], Found]] = [rule_tmp_reference, rule_tmd_numbers, rule_stage_lists, rule_traffic_flow,
                                       rule_tsl_extent, rule_tsl_hours, rule_dates, rule_merged, rule_ratings,
                                       rule_cell_colours]


def run_rules(ctx: Ctx) -> None:
    n = 0
    for rule in RULES:
        result, conflicts = rule(ctx)
        ids = []
        for cf in conflicts:
            n += 1
            cf.conflict_id = f"C-{n:03d}"
            ids.append(cf.conflict_id)
            for path in cf.affected:
                fv = ctx.facts.value(path) if "." in path else None
                if fv is not None:
                    fv.status = Status.CONFLICT
            ctx.facts.conflicts.append(cf)
        result.conflict_id = ", ".join(ids) or None
        ctx.facts.checks.append(result)


def coverage(ctx: Ctx, accounted: set[str]) -> dict:
    """Every element has an outcome, and every record is held by a fact, the register, a mention or ``other``."""
    els = list(ctx.store.elements.values())
    no_outcome = [e.element_id for e in els if e.outcome is None]
    leftover = [e.element_id for e in els if e.outcome == Outcome.LEFTOVER]
    missing = sorted(set(ctx.records.records) - accounted)
    return {"elements": len(els), "without_outcome": no_outcome, "leftover": leftover,
            "records": len(ctx.records), "records_unaccounted": missing,
            "complete": not no_outcome and not missing}
