"""Retry with exponential backoff and jitter.

Used by ingestion, connectors and the job runner. Retrying is only correct for *transient*
failures, so the caller declares which exception types qualify — a schema error must never be
retried, a socket timeout should be.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from gdap.observability.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryConfig:
    attempts: int = 3
    backoff_seconds: float = 2.0
    multiplier: float = 2.0
    max_backoff_seconds: float = 60.0
    jitter: float = 0.1

    def delay_for(self, attempt: int) -> float:
        base = min(
            self.backoff_seconds * (self.multiplier ** (attempt - 1)),
            self.max_backoff_seconds,
        )
        return base * (1 + random.uniform(-self.jitter, self.jitter))  # noqa: S311


def retry_call(
    operation: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    give_up_on: Sequence[type[BaseException]] = (),
    context: str = "operation",
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``operation`` until it succeeds, the attempts run out, or a fatal error is raised."""
    settings = config or RetryConfig()
    last_error: BaseException | None = None

    for attempt in range(1, settings.attempts + 1):
        try:
            return operation()
        except tuple(give_up_on):
            raise
        except tuple(retry_on) as exc:
            last_error = exc
            if attempt >= settings.attempts:
                break
            delay = settings.delay_for(attempt)
            log.warning(
                "retrying",
                context=context,
                attempt=attempt,
                of=settings.attempts,
                delay_seconds=round(delay, 2),
                error=f"{type(exc).__name__}: {exc}",
            )
            sleeper(delay)

    assert last_error is not None
    log.error(
        "retries_exhausted",
        context=context,
        attempts=settings.attempts,
        error=str(last_error),
    )
    raise last_error
