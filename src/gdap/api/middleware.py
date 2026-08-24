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
