"""The handler contract (plan sections 1.6 and part 2).

A handler receives the run context and one routed element and returns a ``HandlerResult``; it never mutates
the store directly. Handlers that need to see more than one element (a table that continues over a page break,
checkbox pairing that needs sibling text) use ``begin`` / ``finish`` to collect and resolve per document.
The router isolates failures: an exception in ``handle`` becomes outcome ``failed`` for that element.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..config import Config
from ..records import Record, RecordStore, Stamp
from .catalog import Catalog, load_catalog
from .models import Element, ElementStore
from .vocab import Flag, Outcome, Route

if TYPE_CHECKING:                       # handlers never import a concrete client or the diagram reader
    from ..llm import ModelClient
    from .extract.diagrams.models import SheetStore


@dataclass
class HandlerContext:
    store: ElementStore
    records: RecordStore
    cfg: Config
    versions: dict[str, str]
    model: ModelClient | None = None
    catalog: Catalog = field(default_factory=load_catalog)
    sheets: SheetStore | None = None       # diagram sheets read by the pre-router diagram stage
    out_dir: Path | None = None            # where masters and overlay renders were written (for image prompts)

    def stamp(self, handler: Handler | BaseHandler, prompts: dict[str, str] | None = None,
              model_name: str = "none") -> Stamp:
        """The version stamp for records this handler produces in this run."""
        return Stamp(type(handler).__name__, handler.version, self.cfg.config_hash(), prompts or {}, model_name,
                     self.catalog.version)


@dataclass
class ElementUpdate:
    add_flags: tuple[Flag, ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass
class HandlerResult:
    records: list[Record] = field(default_factory=list)
    element_updates: dict[str, ElementUpdate] = field(default_factory=dict)
    outcome: Outcome | None = None      # override the router's outcome, for example "failed" handled in-band
    note: str | None = None


@runtime_checkable
class Handler(Protocol):
    route: Route
    version: str

    def begin(self, ctx: HandlerContext) -> None: ...
    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult: ...
    def finish(self, ctx: HandlerContext) -> HandlerResult: ...


class BaseHandler:
    """Default no-op ``begin`` / ``finish``; subclasses set ``route`` and ``version`` and implement ``handle``."""
    route: Route
    version: str = "0.0.0"

    def begin(self, ctx: HandlerContext) -> None:
        return None

    def handle(self, ctx: HandlerContext, element: Element) -> HandlerResult:
        raise NotImplementedError

    def finish(self, ctx: HandlerContext) -> HandlerResult:
        return HandlerResult()


class MissingHandlerError(LookupError):
    """Elements were routed to a route that has no registered handler."""


class HandlerRegistry:
    def __init__(self, handlers: Iterable[Handler] = ()):
        self._by_route: dict[Route, Handler] = {}
        for h in handlers:
            self.register(h)

    def register(self, handler: Handler) -> None:
        if handler.route in self._by_route:
            raise ValueError(f"route {handler.route} already has a handler")
        self._by_route[Route(handler.route)] = handler

    def replace(self, handler: Handler) -> None:
        self._by_route[Route(handler.route)] = handler

    def get(self, route: Route | str) -> Handler:
        try:
            return self._by_route[Route(route)]
        except KeyError:
            raise MissingHandlerError(f"no handler registered for route {route}") from None

    def routes(self) -> set[Route]:
        return set(self._by_route)

    def require(self, routes: Iterable[Route | str]) -> None:
        missing = sorted({str(r) for r in routes} - {str(r) for r in self._by_route})
        if missing:
            raise MissingHandlerError(f"no handler registered for route(s): {', '.join(missing)}")

    def __iter__(self):
        return iter(self._by_route.values())
