"""Phase 1 handler stubs: accept every element routed to them and keep a count per route.

Each real handler in Phase 2 (TEXT, TABLE, RISK_TABLE, CHECKBOX) and Phase 3 (DIAGRAM, FIGURE) takes the
same call shape, so replacing a stub is a one-line change in the registry.
"""
from __future__ import annotations

from collections import Counter

from ..models import Element

ROUTES = ("TEXT", "TABLE", "RISK_TABLE", "CHECKBOX", "FIGURE", "DIAGRAM")


class StubHandler:
    def __init__(self, route: str):
        self.route = route
        self.received: list[str] = []

    def handle(self, element: Element) -> None:
        self.received.append(element.element_id)


def default_registry() -> dict[str, StubHandler]:
    return {r: StubHandler(r) for r in ROUTES}


def received_counts(registry: dict[str, StubHandler]) -> Counter:
    return Counter({r: len(h.received) for r, h in registry.items()})
