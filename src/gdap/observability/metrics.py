"""In-process metrics sink.

Deliberately dependency-free: it satisfies the :class:`MetricsSink` port and exposes a snapshot
for ``/metrics`` and ``gdap system health``. A Prometheus/OTel adapter can replace it without any
call-site change (that is the point of the port).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


class InMemoryMetrics:
    """Thread-safe counters, gauges and histogram summaries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._observations: dict[str, list[float]] = defaultdict(list)
        self._started = time.time()

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[_key(name, labels)] += value

    def gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            bucket = self._observations[_key(name, labels)]
            bucket.append(value)
            if len(bucket) > 10_000:  # bounded memory
                del bucket[:5_000]

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000, **labels)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            summaries = {}
            for key, values in self._observations.items():
                if not values:
                    continue
                ordered = sorted(values)
                count = len(ordered)
                summaries[key] = {
                    "count": count,
                    "avg": sum(ordered) / count,
                    "min": ordered[0],
                    "max": ordered[-1],
                    "p50": ordered[int(count * 0.50)] if count > 1 else ordered[0],
                    "p95": ordered[min(int(count * 0.95), count - 1)],
                }
            return {
                "uptime_seconds": round(time.time() - self._started, 3),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": summaries,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._observations.clear()


METRICS = InMemoryMetrics()
