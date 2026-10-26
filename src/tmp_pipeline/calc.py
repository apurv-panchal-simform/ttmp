"""``calculate`` (plan 5.8): arithmetic on stored values, done by code and shown with its working.

The expression may only name values that came from ``get_facts`` (each with its unit and source). Literal numbers
are rejected, so a model can choose what to combine but can never bring a number of its own. Supported: + - * /,
min, max, abs, round, one comparison (< <= == >= >), unit conversion between km and m, min and h, and date
differences in days.
"""
from __future__ import annotations

import ast
import datetime as dt
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import PipelineError


class CalcError(PipelineError):
    """The expression is not allowed or does not make sense with these units."""


# unit -> (dimension, factor to the dimension's base unit)
UNITS: dict[str, tuple[str, float]] = {
    "m": ("length", 1.0), "km": ("length", 1000.0),
    "min": ("time", 1.0), "h": ("time", 60.0), "hrs": ("time", 60.0), "hours": ("time", 60.0),
    "days": ("days", 1.0), "day": ("days", 1.0),
    "km/h": ("speed", 1.0), "%": ("percent", 1.0), "": ("number", 1.0),
}
_OPS = {ast.Add: ("+", operator.add), ast.Sub: ("−", operator.sub), ast.Mult: ("×", operator.mul),
        ast.Div: ("÷", operator.truediv)}
_CMP = {ast.Lt: ("<", operator.lt), ast.LtE: ("≤", operator.le), ast.Eq: ("=", operator.eq),
        ast.GtE: ("≥", operator.ge), ast.Gt: (">", operator.gt)}
_FUNCS: dict[str, Callable[..., Any]] = {"min": min, "max": max, "abs": abs, "round": round}


@dataclass
class Q:
    """A quantity: a number in a unit, with the decimals it was written with (for the working)."""
    value: float
    unit: str
    decimals: int = 0

    def dim(self) -> str:
        if self.unit not in UNITS:
            raise CalcError(f"unknown unit {self.unit!r}")
        return UNITS[self.unit][0]

    def to(self, unit: str) -> Q:
        if unit == self.unit:
            return self
        if unit not in UNITS or UNITS[unit][0] != self.dim():
            raise CalcError(f"cannot convert {self.unit or 'a plain number'} to {unit}")
        value = self.value * UNITS[self.unit][1] / UNITS[unit][1]
        return Q(value, unit, _decimals_for(value))

    def text(self, decimals: int | None = None) -> str:
        d = self.decimals if decimals is None else decimals
        if d:
            num = f"{self.value:.{d}f}"
        elif float(self.value).is_integer():
            num = str(int(self.value))
        else:
            num = f"{self.value:g}"
        return f"{num} {self.unit}".strip()


def _decimals_for(value: float) -> int:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return len(text.split(".")[1]) if "." in text else 0


def _decimals(raw: object) -> int:
    text = str(raw)
    return len(text.split(".")[1]) if "." in text and text.replace(".", "", 1).lstrip("-").isdigit() else 0


def _date(value: object) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


class _Eval:
    def __init__(self, values: dict[str, dict]):
        self.values = values
        self.used: list[str] = []

    def name(self, node: ast.Name) -> tuple[Q | dt.date, str]:
        if node.id not in self.values:
            raise CalcError(f"{node.id!r} is not one of the values given")
        v = self.values[node.id]
        self.used.append(node.id)
        unit = str(v.get("unit") or "")
        if unit == "date" or (not unit and _date(v.get("value")) is not None and isinstance(v.get("value"), str)):
            d = _date(v.get("value"))
            if d is None:
                raise CalcError(f"{node.id!r} is not a date")
            return d, d.isoformat()
        raw = v.get("value")
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise CalcError(f"{node.id!r} has no number to calculate with ({raw!r})")
        q = Q(float(raw), unit, max(_decimals(raw), _decimals(v.get("display") or "")))
        return q, q.text()

    def run(self, node: ast.AST) -> tuple[Q | dt.date | bool, str]:
        if isinstance(node, ast.Expression):
            return self.run(node.body)
        if isinstance(node, ast.Name):
            return self.name(node)
        if isinstance(node, ast.Constant):
            raise CalcError("numbers may not be written into the expression; use stored values only")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            v, t = self.run(node.operand)
            if not isinstance(v, Q):
                raise CalcError("only a quantity can be negated")
            return Q(-v.value, v.unit, v.decimals), f"−{t}"
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return self.binop(node)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
            left, lt = self.run(node.left)
            right, rt = self.run(node.comparators[0])
            sym, fn = _CMP[type(node.ops[0])]
            if isinstance(left, Q) and isinstance(right, Q):
                right = right.to(left.unit)
                return bool(fn(round(left.value, 9), round(right.value, 9))), f"{lt} {sym} {rt}"
            if isinstance(left, dt.date) and isinstance(right, dt.date):
                return bool(fn(left, right)), f"{lt} {sym} {rt}"
            raise CalcError("cannot compare these values")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            args = [self.run(a) for a in node.args]
            qs = [a for a, _ in args]
            if not qs or not all(isinstance(q, Q) for q in qs):
                raise CalcError(f"{node.func.id} needs quantities")
            if node.func.id in ("abs", "round"):
                if len(qs) != 1:
                    raise CalcError(f"{node.func.id} takes one value")
                q = qs[0]
                assert isinstance(q, Q)
                value = abs(q.value) if node.func.id == "abs" else float(round(q.value))
                return Q(value, q.unit, 0 if node.func.id == "round" else q.decimals), f"{node.func.id}({args[0][1]})"
            first = qs[0]
            assert isinstance(first, Q)
            conv = [q.to(first.unit) for q in qs if isinstance(q, Q)]
            best = _FUNCS[node.func.id](conv, key=lambda q: q.value)
            return best, f"{node.func.id}({', '.join(t for _, t in args)})"
        raise CalcError(f"not allowed in an expression: {type(node).__name__}")

    def binop(self, node: ast.BinOp) -> tuple[Q | dt.date, str]:
        sym, fn = _OPS[type(node.op)]
        left, lt = self.run(node.left)
        right, rt = self.run(node.right)
        text = f"{lt} {sym} {rt}"
        if isinstance(left, dt.date) and isinstance(right, dt.date) and isinstance(node.op, ast.Sub):
            return Q(float((left - right).days), "days"), text
        if not (isinstance(left, Q) and isinstance(right, Q)):
            raise CalcError("dates can only be subtracted from each other")
        if isinstance(node.op, ast.Add | ast.Sub):
            r = right.to(left.unit)
            return Q(fn(left.value, r.value), left.unit, max(left.decimals, r.decimals)), text
        if left.unit and right.unit and not isinstance(node.op, ast.Div):
            raise CalcError("multiplying two quantities with units is not supported")
        if isinstance(node.op, ast.Div):
            if right.value == 0:
                raise CalcError("division by zero")
            if left.unit and right.unit:
                if left.dim() != right.dim():
                    raise CalcError(f"cannot divide {left.unit} by {right.unit}")
                ratio = left.value / right.to(left.unit).value
                return Q(ratio, "", _decimals_for(round(ratio, 3))), text
        value = fn(left.value, right.value)
        return Q(value, left.unit or right.unit, _decimals_for(round(value, 6))), text


def calculate(expression: str, values: dict[str, dict], result_unit: str | None = None,
              stated: dict | None = None) -> dict:
    """{"result", "unit", "working", "sources", "cross_check"?} or {"error"}. ``values`` is name -> {value, unit,
    source, display?}; ``stated`` is the document's own figure for the result ({value, unit, source}), if any."""
    try:
        tree = ast.parse(expression, mode="eval")
        ev = _Eval(values)
        result, text = ev.run(tree)
    except (SyntaxError, CalcError) as e:
        return {"error": str(e), "expression": expression}
    sources = list(dict.fromkeys(str(values[n].get("source")) for n in ev.used if values[n].get("source")))
    if isinstance(result, bool):
        return {"result": result, "unit": "", "working": f"{text}: {'true' if result else 'false'}", "sources": sources}
    if not isinstance(result, Q):
        return {"error": "the expression did not give a number", "expression": expression}
    working = f"{text} = {result.text()}"
    final = result
    if result_unit and result_unit != result.unit:
        try:
            final = result.to(result_unit)
        except CalcError as e:
            return {"error": str(e), "expression": expression}
        working += f" = {final.text()}"
    value: float | int = int(round(final.value)) if abs(final.value - round(final.value)) < 1e-9 else round(final.value, 6)
    out: dict = {"result": value, "unit": final.unit, "working": working, "sources": sources}
    if stated and isinstance(stated.get("value"), int | float) and not isinstance(stated.get("value"), bool):
        try:
            s = Q(float(stated["value"]), str(stated.get("unit") or final.unit)).to(final.unit)
        except CalcError:
            s = None
        if s is not None:
            same = abs(s.value - final.value) < 1e-6
            src = f" ({stated['source']})" if stated.get("source") else ""
            out["cross_check"] = (f"{'matches' if same else 'differs from'} the stated "
                                  f"{Q(float(stated['value']), str(stated.get('unit') or final.unit)).text()}{src}")
    return out


def named_values(fields: dict[str, Any], source: str = "") -> dict[str, dict]:
    """The numbers and ISO dates among a fact's fields, as ``calculate``'s named values. A value in conflict, a flag
    and a missing value are left out, so an expression that needs one gives an error and no figure."""
    out: dict[str, dict] = {}
    for name, fv in fields.items():
        value = getattr(fv, "value", None)
        if value is None or isinstance(value, bool) or str(getattr(fv, "status", "")) == "conflict":
            continue
        if isinstance(value, int | float):
            out[name] = {"value": value, "unit": getattr(fv, "unit", None) or "", "source": source}
        elif isinstance(value, str) and _date(value) is not None:
            out[name] = {"value": value, "unit": "date", "source": source}
    return out
