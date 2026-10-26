"""Handlers receive the elements the router sends them (plan part 2). ``build_registry`` wires the real
handlers; routes whose handler is not built yet (none left after Phase 3) keep counting stubs."""
from __future__ import annotations

from ...contracts import HandlerRegistry
from .checkboxes import CheckboxHandler
from .diagram import DiagramHandler
from .image import FigureHandler
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
    registry.replace(DiagramHandler())
    registry.replace(FigureHandler())
    return registry
