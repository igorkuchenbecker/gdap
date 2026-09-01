"""The request-body ceiling, enforced ahead of the parser.

The per-endpoint check inside the upload handler cannot protect the process: a handler only runs
once FastAPI has resolved its parameters, and resolving an ``UploadFile`` means Starlette has
already parsed and spooled the whole multipart body. Measured before this middleware existed, a
200 MB body against a 1 MB limit was received in full and only then answered 413.

So these tests assert the thing that actually matters — that an oversized body is refused
*without being read* — and not merely that a 413 comes back, which was true all along.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from gdap.api.middleware import BodySizeLimitMiddleware

pytestmark = pytest.mark.integration

_LIMIT = 4096


@pytest.fixture
def counting_app() -> tuple[TestClient, dict[str, int]]:
    """An app behind the ceiling that records how many body bytes ever reached it."""
    seen = {"bytes": 0, "handled": 0}
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        seen["bytes"] += len(body)
        seen["handled"] += 1
        return {"received": len(body)}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=_LIMIT)
    return TestClient(app), seen


def test_a_body_under_the_ceiling_passes_through(counting_app: Any) -> None:
    client, seen = counting_app
    response = client.post("/echo", content=b"x" * (_LIMIT // 2))

    assert response.status_code == 200
    assert response.json()["received"] == _LIMIT // 2
    assert seen["handled"] == 1


def test_a_declared_oversized_body_is_refused_without_reaching_the_app(
    counting_app: Any,
) -> None:
    """Content-Length is the common case: every browser and curl upload sends it."""
    client, seen = counting_app
    response = client.post("/echo", content=b"x" * (_LIMIT * 4))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "GDAP-2004"
    # The point of the whole exercise: the application never saw a byte of it.
    assert seen["handled"] == 0
    assert seen["bytes"] == 0


def test_a_chunked_oversized_body_is_cut_off_mid_stream(counting_app: Any) -> None:
    """Without Content-Length the header cannot be trusted to exist, so bytes are counted.

    A client that simply omits the header would otherwise walk straight past a check that only
    reads it.
    """
    client, seen = counting_app

    def chunks() -> Any:
        for _ in range(40):
            yield b"x" * 1024

    response = client.post("/echo", content=chunks())

    assert response.status_code == 413
    assert seen["handled"] == 0


def test_a_limit_of_zero_disables_the_ceiling(counting_app: Any) -> None:
    """Zero means unlimited, matching how the other limits in this API read."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"received": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=0)
    response = TestClient(app).post("/echo", content=b"x" * (_LIMIT * 4))

    assert response.status_code == 200


def test_the_real_api_refuses_an_oversized_upload_before_parsing(api_client: Any) -> None:
    """End to end on the actual app, asserting *which layer* refused it.

    The upload handler has always answered 413 for an oversized file — after buffering the whole
    thing — so a test that only checks the status code passes with the middleware removed and
    proves nothing. The two layers word the error differently on purpose, and this asserts the
    middleware's wording, which is the only observable difference from outside.
    """
    limit_mb = api_client.app.state.platform.settings.api.max_upload_mb
    oversized = b"a" * (limit_mb * 1024 * 1024 + 1024)

    response = api_client.post(
        "/api/v1/sources/upload",
        files={"file": ("huge.csv", oversized, "text/csv")},
    )

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "GDAP-2004"
    assert error["message"].startswith("request body exceeds"), (
        "413 came from the handler, not the middleware — the body was buffered before it was "
        f"refused: {error['message']!r}"
    )
    assert error["trace_id"], "a refused request should still be traceable"
