"""Handlers that only count what they receive. They stand in for routes whose real handler is not built yet
(FIGURE and DIAGRAM until Phase 3) and let the router be tested without any handler logic."""
from __future__ import annotations

from collections import Counter

from ..contracts import BaseHandler, HandlerContext, HandlerRegistry, HandlerResult
from ..models import Element
from ..vocab import Route

STUB_ROUTES = (Route.TEXT, Route.TABLE, Route.RISK_TABLE, Route.CHECKBOX, Route.FIGURE, Route.DIAGRAM)


class StubHandler(BaseHandler):
    version = "stub"

    def __init__(self, route: Route | str):
        self.route = Route(route)
        self.received: list[str] = []

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        self.received.append(element.element_id)
        return HandlerResult()


def stub_registry(routes=STUB_ROUTES) -> HandlerRegistry:
    return HandlerRegistry(StubHandler(r) for r in routes)


def received_counts(registry: HandlerRegistry) -> Counter:
    return Counter({str(h.route): len(h.received) for h in registry if isinstance(h, StubHandler)})
