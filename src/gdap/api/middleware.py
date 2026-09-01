"""Request middleware: tracing, timing and a simple in-process rate limit."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gdap.observability.logging import get_logger, log_context, new_trace_id
from gdap.observability.metrics import METRICS

log = get_logger(__name__)


class TracingMiddleware(BaseHTTPMiddleware):
    """Assigns a trace id, binds it to every log line of the request, and returns it."""

    def __init__(self, app: Callable[..., Awaitable[None]], header: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header = header

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        trace_id = request.headers.get(self.header) or new_trace_id()
        request.state.trace_id = trace_id
        started = time.perf_counter()

        with log_context(trace_id=trace_id, path=request.url.path, method=request.method):
            response = await call_next(request)

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[self.header] = trace_id
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"

        route = request.scope.get("route")
        endpoint = getattr(route, "path", request.url.path)
        METRICS.observe("http_request_ms", duration_ms, endpoint=endpoint, method=request.method)
        METRICS.increment(
            "http_requests_total",
            endpoint=endpoint,
            method=request.method,
            status=str(response.status_code),
        )
        if response.status_code >= 500 or duration_ms > 5_000:
            log.warning(
                "slow_or_failed_request",
                status=response.status_code,
                duration_ms=round(duration_ms, 1),
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window limiter per API key (or client host).

    In-process on purpose: it protects a single node from a runaway client. A multi-node
    deployment puts a real limiter at the edge — this one never pretends to be that (§53).
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        limit_per_minute: int = 240,
        header: str = "X-API-Key",
        exempt_paths: tuple[str, ...] = ("/health", "/readyz", "/metrics"),
    ) -> None:
        super().__init__(app)
        self.limit = limit_per_minute
        self.header = header
        self.exempt_paths = exempt_paths
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if self.limit <= 0 or request.url.path in self.exempt_paths:
            return await call_next(request)

        key = request.headers.get(self.header) or (
            request.client.host if request.client else "anonymous"
        )
        now = time.time()
        window = self._hits[key[:64]]
        while window and now - window[0] > 60:
            window.popleft()

        if len(window) >= self.limit:
            retry_after = max(1, int(60 - (now - window[0])))
            METRICS.increment("http_rate_limited_total")
            log.warning("rate_limited", key_prefix=key[:8], limit=self.limit)
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "GDAP-3004",
                        "message": f"rate limit exceeded ({self.limit} requests/minute)",
                        "details": {"retry_after_seconds": retry_after},
                        "trace_id": getattr(request.state, "trace_id", None),
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)
        return await call_next(request)


class _BodyTooLargeError(Exception):
    """Raised inside the receive wrapper when a streamed body passes the ceiling."""


class BodySizeLimitMiddleware:
    """Reject an oversized request body *before* anything buffers it.

    The per-endpoint size check in the upload handler cannot protect the process, and measuring
    it showed exactly that: a 200 MB body against a 1 MB limit was received in full and only then
    answered 413. A handler runs after FastAPI has resolved its parameters, and resolving an
    ``UploadFile`` means Starlette has already parsed the multipart body and spooled it — to
    memory below ``spool_max_size`` and to a temporary file above it, which on a host with a
    tmpfs ``/tmp`` is still memory. By the time the handler can object, the damage is done.

    So the ceiling is enforced here, in raw ASGI, ahead of the parser. Two paths, because there
    are two ways a body arrives:

    * ``Content-Length`` present -- the common case, and what every browser and curl upload
      sends. Rejected outright without reading a byte of the body.
    * No ``Content-Length`` (chunked transfer) -- the header cannot be trusted to exist, so the
      receive channel is wrapped and the bytes are counted as the server pulls them. The request
      is abandoned the moment the count passes the ceiling, rather than after the parser has
      assembled the whole thing.

    Written as plain ASGI rather than ``BaseHTTPMiddleware`` for the second path:
    ``BaseHTTPMiddleware`` does not hand you the receive channel, so counting there is not
    possible.

    This is a ceiling for *any* request body, not only uploads. No endpoint in this API has a
    legitimate reason to accept more than the configured upload limit, and a JSON route left
    unguarded is the same hole with a different content type.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, send, declared)
            return

        received = 0
        started = False

        async def counting_receive() -> dict:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            # Only safe to answer while nothing has been written. If the application already
            # started a response it owns the exchange, and the connection is simply dropped.
            if not started:
                await self._reject(scope, send, None)

    async def _reject(self, scope: dict, send: Callable, declared: int | None) -> None:
        limit_mb = self.max_bytes / (1024 * 1024)
        # Set by TracingMiddleware, which wraps this one; absent only if this middleware is
        # ever moved outside it, and reported as null rather than invented in that case.
        trace_id = scope.get("state", {}).get("trace_id")
        METRICS.increment("http_payload_too_large_total")
        log.warning("payload_too_large", limit_bytes=self.max_bytes, declared=declared)
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "GDAP-2004",
                    "message": f"request body exceeds the {limit_mb:.0f} MB limit",
                    "details": {"limit_mb": round(limit_mb, 3)},
                    "trace_id": trace_id,
                }
            },
        )
        await response(scope, _no_body_receive, send)


async def _no_body_receive() -> dict:
    """A receive channel for responding without touching the request body."""
    return {"type": "http.disconnect"}


def _content_length(scope: dict) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
