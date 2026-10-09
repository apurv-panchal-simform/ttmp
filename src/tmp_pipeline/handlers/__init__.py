"""Handlers receive the elements the router sends them (plan part 2). ``build_registry`` wires the real
handlers; routes whose handler is not built yet (FIGURE, DIAGRAM until Phase 3) keep counting stubs."""
from __future__ import annotations

from ..contracts import HandlerRegistry
from .checkboxes import CheckboxHandler
from .risk import RiskTableHandler
from .stubs import stub_registry
from .tables import TableHandler
from .text import TextHandler


def build_registry() -> HandlerRegistry:
    registry = stub_registry()
    registry.replace(CheckboxHandler())
    registry.replace(TextHandler())
    registry.replace(TableHandler())
    registry.replace(RiskTableHandler())
    return registry
