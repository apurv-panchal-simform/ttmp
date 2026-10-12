"""Score a run's records against a gold file (plan section 7.1).

Gold file: ``{"expected": [{"kind", "page", "match": {field: value}, "fields": {field: value},
"entity", "applicability", "unverified": [field], "flags": [flag]}], "notes": "..."}``. A gold record is found by
kind, page and the ``match`` entries (typed field values; ``data.<key>`` matches record data such as a section
heading). Measures:

  field accuracy             expected fields whose typed value equals the gold value (target >= 98%)
  wrong marked verified      wrong values whose status is ``verified``; critical fields counted separately (target 0)
  wrong flagged unverified   share of wrong values stored ``unverified`` (target 100%)
  expected unverified hit    fields the gold says must be unverified (a glitchy cell, an unjudged critical fact)
  invented fields            output fields whose text is not found in the elements they cite (target 0)
  coverage                   gold records found (target 100%)
  sheet facts exact          per diagram sheet, legend, notes and roads equal the gold lists (target 100%)
  symbol recall / precision  gold ``sheets[].symbols`` specs ({"kind", optional "value", "rp", "text", "n"}) found among
                             a sheet's symbols; precision counts only symbols of kinds the gold lists (target >= 95%)

Conflicts and approval status are Phase 4 and not scored here."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..catalog import Catalog, load_catalog
from ..models import ElementStore
from ..records import FieldValue, Record, RecordStore

GRAIN = re.compile(r"\w+", re.UNICODE)
DERIVED = ("_check", "_note")                  # fields the handlers compute rather than read


@dataclass
class FieldResult:
    gold_id: str
    field: str
    expected: object
    actual: object
    status: str | None
    outcome: str                        # correct | wrong | missing


@dataclass
class GoldScore:
    results: list[FieldResult] = field(default_factory=list)
    missing_records: list[str] = field(default_factory=list)
    n_records: int = 0
    wrong_verified: list[FieldResult] = field(default_factory=list)
    wrong_verified_critical: list[FieldResult] = field(default_factory=list)
    unverified_expected: list[tuple[str, str, bool]] = field(default_factory=list)   # (gold_id, field, hit)
    flag_expected: list[tuple[str, str, bool]] = field(default_factory=list)
    invented: list[tuple[str, str, str]] = field(default_factory=list)               # (record_id, field, raw)
    sheet_checks: list[tuple[str, str, bool]] = field(default_factory=list)          # (gold_id, what, ok)
    symbols_expected: int = 0
    symbols_matched: int = 0
    symbols_missing: list[str] = field(default_factory=list)
    symbols_extra: list[str] = field(default_factory=list)       # produced, of a gold kind, but not in gold

    @property
    def n_fields(self) -> int:
        return len(self.results)

    @property
    def accuracy(self) -> float:
        return sum(r.outcome == "correct" for r in self.results) / self.n_fields if self.n_fields else 1.0

    @property
    def wrong(self) -> list[FieldResult]:
        return [r for r in self.results if r.outcome != "correct"]

    @property
    def wrong_flagged_rate(self) -> float:
        w = self.wrong
        return 1.0 if not w else sum(r.status == "unverified" for r in w) / len(w)

    @property
    def coverage(self) -> float:
        return (self.n_records - len(self.missing_records)) / self.n_records if self.n_records else 1.0

    @property
    def unverified_hit_rate(self) -> float:
        u = self.unverified_expected
        return 1.0 if not u else sum(h for _, _, h in u) / len(u)

    @property
    def flag_hit_rate(self) -> float:
        f = self.flag_expected
        return 1.0 if not f else sum(h for _, _, h in f) / len(f)

    @property
    def sheet_exact_rate(self) -> float:
        c = self.sheet_checks
        return 1.0 if not c else sum(ok for _, _, ok in c) / len(c)

    @property
    def symbol_recall(self) -> float:
        return self.symbols_matched / self.symbols_expected if self.symbols_expected else 1.0

    @property
    def symbol_precision(self) -> float:
        produced = self.symbols_matched + len(self.symbols_extra)
        return self.symbols_matched / produced if produced else 1.0

    def passes(self) -> bool:
        return (self.accuracy >= 0.98 and not self.wrong_verified and not self.invented and self.coverage == 1.0
                and self.wrong_flagged_rate == 1.0 and self.unverified_hit_rate == 1.0 and self.flag_hit_rate == 1.0
                and self.sheet_exact_rate == 1.0 and self.symbol_recall >= 0.95 and self.symbol_precision >= 0.95)


def _norm(v: object) -> object:
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _equal(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 1e-9
    return _norm(a) == _norm(b)


def _matches(rec: Record, key: str, want: object) -> bool:
    if key.startswith("data."):                    # sections are found by heading, which is data, not a field
        return _equal(rec.data.get(key[5:]), want)
    return key in rec.fields and _equal(rec.fields[key].value, want)


def _find(records: RecordStore, g: dict) -> Record | None:
    for rec in records.of_kind(g["kind"], page=g["page"]):
        if all(_matches(rec, k, v) for k, v in g.get("match", {}).items()):
            return rec
    return None


def _grounded(raw: str, texts: list[str]) -> bool:
    """Every word of the raw text appears in the cited elements' text (continuations join several elements)."""
    have = {w.lower() for t in texts for w in GRAIN.findall(t)}
    return all(w.lower() in have for w in GRAIN.findall(raw))


def _nospace(v: object) -> str:
    return "".join(str(v).split()).lower()


def _symbol_matches(sym: dict, spec: dict) -> bool:
    if sym.get("kind") != spec["kind"]:
        return False
    if "value" in spec and not _equal(sym.get("value"), spec["value"]):
        return False
    if "rp" in spec and not (sym.get("rp") is not None and _equal(sym["rp"], spec["rp"])):
        return False
    return not ("text" in spec and _nospace(sym.get("text", "")) != _nospace(spec["text"]))


def _describe(spec: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in spec.items() if k != "n")


def _score_sheets(records: RecordStore, gold: dict, out: GoldScore) -> None:
    for sg in gold.get("sheets", []):
        gid = f"sheet:p{sg['page']}"
        rec = next(iter(records.of_kind("diagram_sheet", page=sg["page"])), None)
        specs = [dict(s) for s in sg.get("symbols", [])]
        expanded = [{k: v for k, v in s.items() if k != "n"} for s in specs for _ in range(int(s.get("n", 1)))]
        out.symbols_expected += len(expanded)
        if rec is None:
            for what in ("legend", "notes", "roads"):
                if what in sg:
                    out.sheet_checks.append((gid, what, False))
            out.symbols_missing += [f"{gid}: {_describe(s)}" for s in expanded]
            continue
        d = rec.data
        if "legend" in sg:
            out.sheet_checks.append((gid, "legend", [list(x) for x in sg["legend"]] ==
                                     [[e["swatch"], e["meaning"]] for e in d.get("legend", [])]))
        if "notes" in sg:
            out.sheet_checks.append((gid, "notes", [_norm(n) for n in sg["notes"]] == [_norm(n) for n in d.get("notes", [])]))
        if "roads" in sg:
            out.sheet_checks.append((gid, "roads", sorted(map(tuple, sg["roads"])) ==
                                     sorted((r["name"], r["level"]) for r in d.get("roads", []))))
        left = list(d.get("symbols", []))
        for spec in sorted(expanded, key=lambda s: -len(s)):            # the most specific specs claim symbols first
            hit = next((s for s in left if _symbol_matches(s, spec)), None)
            if hit is None:
                out.symbols_missing.append(f"{gid}: {_describe(spec)}")
            else:
                left.remove(hit)
                out.symbols_matched += 1
        kinds = {s["kind"] for s in expanded}
        out.symbols_extra += [f"{gid}: {s['kind']} value={s.get('value')} rp={s.get('rp')} text={s.get('text', '')[:30]!r}"
                              for s in left if s["kind"] in kinds]


def score(records: RecordStore, store: ElementStore, gold: dict, catalog: Catalog | None = None) -> GoldScore:
    catalog = catalog or load_catalog()
    out = GoldScore()
    _score_sheets(records, gold, out)
    gold_kinds_pages = {(g["kind"], g["page"]) for g in gold["expected"]}
    for i, g in enumerate(gold["expected"]):
        gid = g.get("id") or f"{g['kind']}:p{g['page']}:{i}"
        out.n_records += 1
        rec = _find(records, g)
        if rec is None:
            out.missing_records.append(gid)
            continue
        for fname, want in g.get("fields", {}).items():
            fv: FieldValue | None = rec.fields.get(fname)
            if fv is None:
                out.results.append(FieldResult(gid, fname, want, None, None, "missing"))
                continue
            ok = _equal(fv.value, want)
            res = FieldResult(gid, fname, want, fv.value, str(fv.status), "correct" if ok else "wrong")
            out.results.append(res)
            if not ok and fv.status == "verified":
                out.wrong_verified.append(res)
                if rec.entity and catalog.is_critical(rec.entity, fname):
                    out.wrong_verified_critical.append(res)
        for key, actual in (("entity", rec.entity), ("applicability", rec.applicability)):
            if key in g:                           # record-level expectations count as fields
                ok = _equal(actual, g[key])
                out.results.append(FieldResult(gid, key, g[key], actual, str(rec.status), "correct" if ok else "wrong"))
        for fname in g.get("unverified", []):
            fv = rec.fields.get(fname)
            out.unverified_expected.append((gid, fname, fv is not None and fv.status == "unverified"))
        for flag in g.get("flags", []):
            out.flag_expected.append((gid, flag, flag in rec.flags))
    for rec in records.records.values():           # invented fields: only where gold looks (kinds and pages it covers)
        if (rec.kind, rec.page) not in gold_kinds_pages:
            continue
        texts = [store.elements[e.element_id].representations.get("text", "") for e in rec.evidence
                 if e.element_id in store.elements]
        for fname, fv in rec.fields.items():
            if fv.raw and not fname.endswith(DERIVED) and fname != "group" and not _grounded(fv.raw, texts):
                out.invented.append((rec.record_id, fname, fv.raw))
    return out


def render(s: GoldScore) -> str:
    pct = lambda x: f"{x:.1%}"      # noqa: E731
    lines = ["| Measure | Result | Target |", "| --- | --- | --- |",
             f"| Field accuracy | {pct(s.accuracy)} ({s.n_fields - len(s.wrong)}/{s.n_fields}) | >= 98% |",
             f"| Wrong values marked verified | {len(s.wrong_verified)} (critical: {len(s.wrong_verified_critical)}) | 0 |",
             f"| Wrong values flagged unverified | {pct(s.wrong_flagged_rate)} | 100% |",
             f"| Expected-unverified fields flagged | {pct(s.unverified_hit_rate)} ({len(s.unverified_expected)}) | 100% |",
             f"| Expected flags present | {pct(s.flag_hit_rate)} ({len(s.flag_expected)}) | 100% |",
             f"| Invented fields | {len(s.invented)} | 0 |",
             f"| Gold records found | {pct(s.coverage)} ({s.n_records - len(s.missing_records)}/{s.n_records}) | 100% |"]
    if s.sheet_checks or s.symbols_expected:
        lines += [f"| Sheet facts exact (legend, notes, roads) | {pct(s.sheet_exact_rate)} ({len(s.sheet_checks)}) | 100% |",
                  f"| Symbol recall | {pct(s.symbol_recall)} ({s.symbols_matched}/{s.symbols_expected}) | >= 95% |",
                  f"| Symbol precision | {pct(s.symbol_precision)} ({s.symbols_matched}/{s.symbols_matched + len(s.symbols_extra)}) | >= 95% |"]
    if s.wrong:
        lines += ["", "Wrong or missing fields:", ""]
        lines += [f"- {r.gold_id} `{r.field}`: expected {r.expected!r}, got {r.actual!r} ({r.outcome}, {r.status})" for r in s.wrong]
    if s.missing_records:
        lines += ["", "Gold records not found: " + ", ".join(s.missing_records)]
    lines += [f"- {gid} `{what}` differs from the gold list" for gid, what, ok in s.sheet_checks if not ok]
    lines += [f"- symbol not found: {m}" for m in s.symbols_missing] + [f"- symbol not in gold: {m}" for m in s.symbols_extra]
    for gid, fname, hit in s.unverified_expected + s.flag_expected:
        if not hit:
            lines.append(f"- {gid} `{fname}`: expected unverified or flag not present")
    if s.invented:
        lines += ["", "Fields not grounded in the elements they cite:", ""]
        lines += [f"- {rid} `{f}`: {raw!r}" for rid, f, raw in s.invented]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tmp-pipeline-gold", description="Score a run against the gold set")
    ap.add_argument("doc_dir", help="out/<doc_id> folder holding elements.json and records.json")
    ap.add_argument("gold", help="gold JSON file")
    a = ap.parse_args(argv)
    d = Path(a.doc_dir)
    s = score(RecordStore.load(d / "records.json"), ElementStore.load(d / "elements.json"),
              json.loads(Path(a.gold).read_text(encoding="utf-8")))
    print(render(s))
    return 0 if s.passes() else 1


if __name__ == "__main__":
    raise SystemExit(main())
