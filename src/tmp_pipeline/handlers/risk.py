"""Risk matrix and register (plan section 2.3).

Matrix  the 5x5 grid is read from geometry, not from rules: score cells (a number 1..25 on a coloured fill)
        are placed by the x of the impact-column labels and the y of the likelihood-row labels, across the
        page break. Each cell becomes (likelihood, impact, score, colour, rating). The rating comes from the
        cell colour and the document's own key (the impact table: swatch, name, WHS description, action), by
        nearest colour, because two yellows exist.
Register one record per hazard row, read with the generic table machinery (header names -> the ``risk`` entity,
        stitched across pages).
Check   the stated rating is compared with the rating of the matrix cell at (likelihood, impact). The rating is
        NOT a function of the score: 4 is yellow at Likely x Insignificant but green at Unlikely x Low. A
        difference is flagged ``rating_mismatch`` with both values and never corrected.
Links   the affected-TMD column becomes diagram ids (TMD-02, ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .. import logging_ as lg
from ..catalog import normalise
from ..contracts import HandlerContext, HandlerResult
from ..geometry import centre, centre_in
from ..models import Element
from ..numerics import extract
from ..records import Evidence, FieldValue, Record, Status
from ..vocab import Flag, Route
from .grid import CellLine
from .tables import TableHandler, TableInfo

_LIKELIHOOD = re.compile(r"^\s*([1-5])\s*[-–—]\s*(very unlikely|unlikely|possible|very likely|likely)\s*$", re.I)
_IMPACT = re.compile(r"^\s*([1-5])\s*[-–—]\s*(insignificant|low|medium|high|very high)\s*$", re.I)
_SCORE = re.compile(r"^\d{1,2}$")
RATINGS = ("very high", "high", "medium", "low", "insignificant")


@dataclass
class KeyEntry:
    rating: str                      # canonical, e.g. "Very High"
    level: int
    rgb: tuple[float, float, float]
    whs: str = ""
    action: str = ""
    elements: list[Element] = field(default_factory=list)


@dataclass
class MatrixCell:
    likelihood: int
    impact: int
    score: int
    rgb: tuple[float, float, float]
    rating: str | None
    element: Element
    page: int


def _dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def _row_overlap(a: list[float], b: list[float], slack: float) -> bool:
    return a[1] - slack <= b[3] and a[3] + slack >= b[1]


def canonical_rating(text: str) -> str | None:
    n = normalise(text)
    return next((r.title() for r in RATINGS if n == r), None)


class RiskTableHandler(TableHandler):
    route = Route.RISK_TABLE
    version = "1.0.0"

    # --- geometry-based reading ------------------------------------------------------------------------------------

    def _all_lines(self) -> list[tuple[TableInfo, CellLine]]:
        return [(info, ln) for info in self._tables for cell in info.grid.cells for ln in cell.lines]

    def _fill_at(self, info_list: list[TableInfo], page: int, box: list[float]):
        best, best_area = None, 1e18
        for info in info_list:
            if info.element.page != page:
                continue
            for f in info.element.metadata.get("fills", []):
                fb = f["bbox"]
                area = (fb[2] - fb[0]) * (fb[3] - fb[1])
                if centre_in(box, fb) and area < best_area:
                    best, best_area = (f["rgb"][0], f["rgb"][1], f["rgb"][2]), area
        return best

    def _read_matrix(self, ctx: HandlerContext):
        rk = ctx.cfg.risk
        lines = self._all_lines()
        likelihoods = []          # (level, name, line, page)
        impacts = []
        for info, ln in lines:
            if m := _LIKELIHOOD.match(ln.text):
                likelihoods.append((int(m[1]), m[2].title(), ln, info.element.page))
            elif m := _IMPACT.match(ln.text):
                impacts.append((int(m[1]), m[2].title(), ln, info.element.page))
        if not likelihoods or not impacts:
            return None
        cols = {lvl: centre(ln.bbox)[0] for lvl, _, ln, _ in impacts}
        infos = [i for i, _ in lines if i]
        infos = list({id(i): i for i in infos}.values())
        cells: list[MatrixCell] = []
        used: set[int] = set()
        for info, ln in lines:
            if not _SCORE.match(ln.text) or not 1 <= int(ln.text) <= 25:
                continue
            cx, cy = centre(ln.bbox)
            impact = min(cols, key=lambda k: abs(cols[k] - cx))
            if abs(cols[impact] - cx) > rk.column_tol_pt:
                continue
            rows = [(lvl, ln2) for lvl, _, ln2, pg in likelihoods if pg == info.element.page
                    and abs(centre(ln2.bbox)[1] - cy) <= rk.row_tol_pt]
            if not rows:
                continue
            lvl = min(rows, key=lambda r: abs(centre(r[1].bbox)[1] - cy))[0]
            rgb = self._fill_at(infos, info.element.page, ln.bbox)
            if rgb is None:
                continue
            cells.append(MatrixCell(lvl, impact, int(ln.text), rgb, None, ln.element, info.element.page))
            used.add(id(ln))
        return likelihoods, impacts, cells, used, lines, infos

    def _read_key(self, ctx: HandlerContext, lines, used, infos) -> dict[str, KeyEntry]:
        """Impact key rows: a digit 1-5 on a coloured swatch, then the rating name, then the WHS text and action."""
        key: dict[str, KeyEntry] = {}
        for info, ln in lines:
            if id(ln) in used or not re.fullmatch(r"[1-5]", ln.text):
                continue
            rgb = self._fill_at(infos, info.element.page, ln.bbox)
            if rgb is None:
                continue
            row = sorted((x for i, x in lines if i.element.page == info.element.page and x is not ln
                          and _row_overlap(x.bbox, ln.bbox, 1) and x.bbox[0] > ln.bbox[2] - 1), key=lambda x: x.bbox[0])
            if not row or canonical_rating(row[0].text) is None:
                continue
            entry = KeyEntry(canonical_rating(row[0].text) or "", int(ln.text), rgb,
                             row[1].text if len(row) > 1 else "", row[2].text if len(row) > 2 else "",
                             [ln.element, row[0].element] + [x.element for x in row[1:3]])
            key[normalise(entry.rating)] = entry
        return key

    def _rating_of(self, ctx: HandlerContext, key: dict[str, KeyEntry], rgb) -> str | None:
        if not key:
            return None
        best = min(key.values(), key=lambda k: _dist(k.rgb, rgb))
        return best.rating if _dist(best.rgb, rgb) <= ctx.cfg.risk.colour_tol else None

    # --- records ---------------------------------------------------------------------------------------------------

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        result = super().finish(ctx)
        stamp = ctx.stamp(self)
        read = self._read_matrix(ctx)
        matrix: dict[tuple[int, int], MatrixCell] = {}
        key: dict[str, KeyEntry] = {}
        likelihood_levels: dict[str, int] = {}
        if read is not None:
            likelihoods, impacts, cells, used, lines, infos = read
            key = self._read_key(ctx, lines, used, infos)
            likelihood_levels = {normalise(name): lvl for lvl, name, _, _ in likelihoods}
            for c in cells:
                c.rating = self._rating_of(ctx, key, c.rgb)
                matrix[(c.likelihood, c.impact)] = c
            result.records += self._matrix_records(ctx, stamp, matrix, key, likelihoods, impacts)
        self._check_register(ctx, result, matrix, key, likelihood_levels)
        lg.log_event("risk", "done", matrix_cells=len(matrix), key=sorted(key),
                     register_rows=len([r for r in result.records if r.kind == "risk"]))
        return result

    def _matrix_records(self, ctx, stamp, matrix, key, likelihoods, impacts) -> list[Record]:
        out: list[Record] = []
        cells = sorted(matrix.values(), key=lambda c: (-c.likelihood, c.impact))
        page = min((c.page for c in cells), default=1)
        complete = len(cells) == 25
        out.append(Record("risk_matrix", page, stamp, {}, "risk_matrix", [Evidence.of(c.element) for c in cells],
                          status=Status.VERIFIED if complete else Status.UNVERIFIED,
                          data={"complete": complete, "cells": [self._cell_dict(c) for c in cells],
                                "likelihoods": {name: lvl for lvl, name, _, _ in likelihoods},
                                "impacts": {name: lvl for lvl, name, _, _ in impacts},
                                "key": {k.rating: {"level": k.level, "rgb": list(k.rgb), "whs": k.whs,
                                                   "action": k.action} for k in key.values()}}))
        for c in cells:
            problems = []
            if c.score != c.likelihood * c.impact:
                problems.append(f"score {c.score} is not likelihood x impact ({c.likelihood * c.impact})")
            if c.rating is None:
                problems.append("cell colour matches no key colour")
            status = Status.UNVERIFIED if problems else Status.VERIFIED
            out.append(Record("risk_cell", c.page, stamp, {
                "likelihood": FieldValue(c.likelihood), "impact": FieldValue(c.impact),
                "score": FieldValue(c.score, raw=str(c.score), status=status, notes=problems),
                "rating": FieldValue(c.rating, status=status, method="code", notes=problems)},
                "risk_matrix", [Evidence.of(c.element)], status=status, data=self._cell_dict(c)))
        for k in key.values():
            out.append(Record("risk_impact_key", min(e.page for e in k.elements), stamp, {
                "rating": FieldValue(k.rating), "impact": FieldValue(k.level), "whs": FieldValue(k.whs, raw=k.whs),
                "action": FieldValue(k.action, raw=k.action)}, "risk_matrix", [Evidence.of(e) for e in k.elements],
                data={"rgb": list(k.rgb)}))
        return out

    @staticmethod
    def _cell_dict(c: MatrixCell) -> dict:
        return {"likelihood": c.likelihood, "impact": c.impact, "score": c.score, "colour": list(c.rgb),
                "rating": c.rating, "page": c.page, "element_id": c.element.element_id}

    # --- register ----------------------------------------------------------------------------------------------------

    def _impact_levels(self, ctx: HandlerContext, key: dict[str, KeyEntry], text: str) -> list[int]:
        """Impact levels named by a consequence cell: by key description (fuzzy), possibly several when the
        cell stacks values."""
        norm = normalise(text)
        if not norm or not key:
            return []
        scored = []
        for k in key.values():
            for cand in (normalise(k.whs), normalise(k.rating)):
                if cand:
                    scored.append((SequenceMatcher(None, norm, cand).ratio(), k.level, cand))
        found: list[tuple[int, int]] = []
        for k in key.values():                                   # several descriptions inside one stacked cell
            d = normalise(k.whs)
            if d and d in norm:
                found.append((norm.index(d), k.level))
        if len(found) > 1:
            return [lvl for _, lvl in sorted(found)]
        best = max(scored, default=(0.0, 0, ""))
        return [best[1]] if best[0] >= ctx.cfg.risk.description_match else []

    def _likelihood_levels(self, levels: dict[str, int], text: str) -> list[int]:
        norm = normalise(text)
        out = []
        for name in sorted(levels, key=len, reverse=True):
            for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", norm):
                out.append((m.start(), levels[name]))
                norm = norm[:m.start()] + " " * (m.end() - m.start()) + norm[m.end():]
        return [lvl for _, lvl in sorted(out)]

    def _check_register(self, ctx, result: HandlerResult, matrix, key, likelihood_levels) -> None:
        for rec in result.records:
            if rec.kind != "risk":
                continue
            tmd = rec.fields.get("affected_tmds")
            if tmd and tmd.raw:
                ids = [n.value for n in extract(tmd.raw) if n.kind == "tmd"]
                if ids:
                    tmd.value, tmd.method = ids, "pattern"
                    rec.data["links"] = ids
            if not matrix:
                continue
            for stage in ("initial", "residual"):
                self._check_stage(ctx, rec, stage, matrix, key, likelihood_levels)

    def _check_stage(self, ctx, rec: Record, stage: str, matrix, key, likelihood_levels) -> None:
        lik, con, risk = (rec.fields.get(f"{stage}_{n}") for n in ("likelihood", "consequence", "risk"))
        if not (lik and con and risk and lik.raw and con.raw and risk.raw):
            return
        ls = self._likelihood_levels(likelihood_levels, lik.raw)
        cs = self._impact_levels(ctx, key, con.raw)
        stated = [canonical_rating(m.group(0)) for m in re.finditer(r"very high|high|medium|low|insignificant", risk.raw, re.I)]
        if not ls or not cs or not stated or not (len(ls) == len(cs) == len(stated)):
            rec.fields[f"{stage}_check"] = FieldValue(
                None, status=Status.UNVERIFIED, method="code",
                notes=["could not pair likelihood, consequence and stated rating for a lookup"])
            return
        results = []
        for L, I, st in zip(ls, cs, stated, strict=True):
            cell = matrix.get((L, I))
            if cell is None:
                results.append({"likelihood": L, "impact": I, "stated": st, "matrix": None, "score": None})
                continue
            results.append({"likelihood": L, "impact": I, "score": cell.score, "stated": st, "matrix": cell.rating})
            if cell.rating is not None and st is not None and cell.rating != st:
                rec.flags = list(dict.fromkeys([*rec.flags, Flag.RATING_MISMATCH]))
                for fv in (risk,):
                    fv.notes.append(f"rating_mismatch: stated {st}, matrix cell (likelihood {L}, impact {I}) says {cell.rating}")
        rec.fields[f"{stage}_check"] = FieldValue(results, method="code")
        rec.data.setdefault("rating_checks", {})[stage] = results
