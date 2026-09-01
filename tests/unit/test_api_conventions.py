"""Structural invariants of the HTTP layer.

These assert conventions that are cheap to break and expensive to notice. A
handler that violates one still returns 200 in every functional test; what it
breaks is the behaviour of the process under concurrency, which no
single-request test observes.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Iterator
from typing import Any

from fastapi.routing import APIRoute

from gdap.api.app import create_app


def _collect(routes: Iterable[Any]) -> Iterator[APIRoute]:
    """Yield every :class:`APIRoute` reachable from ``routes``.

    Recursive, and it looks in two places on purpose. FastAPI 0.141 keeps an included router as
    a single ``_IncludedRouter`` wrapper in ``app.routes`` rather than flattening its routes into
    it, and that wrapper exposes the real router as ``original_router``, not as ``routes``. A
    flat ``isinstance`` scan therefore finds exactly one route -- the SPA index -- and silently
    checks nothing at all, which is what :func:`test_there_are_routes_to_check` exists to catch.

    Both attributes are tried so this keeps working whichever way a future version arranges them.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        for attribute in ("routes", "original_router"):
            nested = getattr(route, attribute, None)
            if nested is None:
                continue
            yield from _collect(getattr(nested, "routes", nested))


def _routes() -> list[APIRoute]:
    return list(_collect(create_app().routes))


def test_there_are_routes_to_check() -> None:
    """Guard the guard: a scan that reaches no route would pass every check below."""
    assert len(_routes()) > 50


def test_every_handler_is_synchronous() -> None:
    """No route handler may be ``async def``.

    Every service call in this codebase blocks: SQLAlchemy is synchronous, ingestion reads files
    and writes Parquet, and the analytics engine is CPU-bound. FastAPI runs a plain ``def``
    handler in its threadpool, where that is fine, and an ``async def`` handler *on the event
    loop*, where it is not -- one slow request there stalls every other request in the process,
    health checks included.

    A handler that legitimately awaits async I/O end to end would be a reasonable exception, and
    the way to make it is to change this test deliberately rather than to discover the stall in
    production. ``POST /api/v1/sources/upload`` was such a handler: it was ``async`` only to call
    ``await UploadFile.read``, and dragged a full ingestion onto the loop with it.
    """
    offenders = [
        f"{sorted(route.methods)[0]} {route.path} -> {route.endpoint.__qualname__}"
        for route in _routes()
        if inspect.iscoroutinefunction(route.endpoint)
    ]
    assert offenders == [], (
        "these handlers run on the event loop and will stall the process if they block:\n  "
        + "\n  ".join(offenders)
    )
