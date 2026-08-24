"""Uniform error envelope.

Domain errors carry their own code and HTTP status, so handlers translate rather than decide.
Unexpected exceptions never leak internals to the client — they are logged with the trace id the
client received, which is what makes a support conversation possible.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gdap.core.errors import GdapError
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS

log = get_logger(__name__)


def _trace_id(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GdapError)
    async def _domain_error(request: Request, exc: GdapError) -> JSONResponse:
        METRICS.increment("api_errors_total", code=exc.code)
        log.warning(
            "api_domain_error",
            code=exc.code,
            status=exc.http_status,
            path=request.url.path,
            message=exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict(_trace_id(request)))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details: list[dict[str, Any]] = [
            {
                "field": ".".join(str(part) for part in error.get("loc", [])[1:]),
                "problem": error.get("msg"),
                "type": error.get("type"),
            }
            for error in exc.errors()[:20]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "GDAP-2002",
                    "message": "request validation failed",
                    "details": {"errors": details},
                    "trace_id": _trace_id(request),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP-{exc.status_code}",
                    "message": str(exc.detail),
                    "details": {},
                    "trace_id": _trace_id(request),
                }
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = _trace_id(request)
        METRICS.increment("api_errors_total", code="GDAP-1000")
        log.exception(
            "api_unhandled_error",
            path=request.url.path,
            method=request.method,
            trace_id=trace_id,
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "GDAP-1000",
                    "message": "internal server error",
                    "details": {"hint": "quote the trace id when reporting this"},
                    "trace_id": trace_id,
                }
            },
        )
