"""Structured logging.

Console-friendly in development, JSON in production, always carrying the ambient context
(trace id, org, job, step) so a single log line answers *what failed, where and for whom*.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

# A ContextVar default must be immutable: a shared dict would leak bindings across contexts.
_context: ContextVar[dict[str, Any] | None] = ContextVar("gdap_log_context", default=None)
_configured = False


def _inject_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in (_context.get() or {}).items():
        event_dict.setdefault(key, value)
    return event_dict


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
) -> None:
    """Idempotent global logging setup. Safe to call from API, CLI and worker entrypoints."""
    global _configured

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "urllib3", "asyncio", "watchfiles", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "gdap") -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind values (trace_id, org_id, job_id, step) to every log line inside the block.

    Entry and exit can legitimately happen in different contexts — FastAPI runs synchronous
    dependencies in a threadpool, so the ``finally`` may execute elsewhere. Resetting the token
    then raises, so restoring by value is the correct fallback rather than a bug to swallow.
    """
    previous = dict(_context.get() or {})
    merged = {**previous, **{k: v for k, v in values.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        try:
            _context.reset(token)
        except ValueError:
            _context.set(previous)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]
