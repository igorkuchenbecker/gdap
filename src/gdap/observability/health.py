"""System self-diagnostics (§47).

``gdap doctor`` and ``GET /health?deep=true`` run the same checks. Each check is isolated: one
failing subsystem produces one failing check, never an exception that hides the rest.
"""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING, Any

from gdap import __version__
from gdap.core.contracts import HealthCheck, SystemHealth
from gdap.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from gdap.core.container import Platform

log = get_logger(__name__)


def _timed(name: str, probe: Any) -> HealthCheck:
    started = time.perf_counter()
    try:
        message, details = probe()
        return HealthCheck(
            component=name,
            ok=True,
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            details=details or {},
        )
    except Exception as exc:
        return HealthCheck(
            component=name,
            ok=False,
            message=f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def check_platform(platform: Platform, *, deep: bool = False) -> SystemHealth:
    settings = platform.settings
    checks: list[HealthCheck] = []

    def database() -> tuple[str, dict[str, Any]]:
        latency = platform.db.ping()
        return f"reachable in {latency:.1f}ms", {"url": platform.db._safe_url()}

    def filesystem() -> tuple[str, dict[str, Any]]:
        paths = settings.paths
        missing = [
            str(path)
            for path in (paths.home, paths.warehouse, paths.artifacts, paths.models, paths.staging)
            if path is not None and not path.exists()
        ]
        if missing:
            raise RuntimeError(f"missing directories: {', '.join(missing)}")
        usage = shutil.disk_usage(str(paths.home))
        free_pct = usage.free / usage.total * 100
        if free_pct < 5:
            raise RuntimeError(f"only {free_pct:.1f}% disk space left")
        return (
            f"{free_pct:.0f}% free on {paths.home}",
            {"free_gb": round(usage.free / 1e9, 2), "total_gb": round(usage.total / 1e9, 2)},
        )

    def connectors() -> tuple[str, dict[str, Any]]:
        keys = platform.registry.keys()
        if not keys:
            raise RuntimeError("no connectors registered")
        return f"{len(keys)} connector(s) available", {"connectors": keys}

    def steps() -> tuple[str, dict[str, Any]]:
        from gdap.pipelines.steps import known_steps

        available = known_steps()
        if not available:
            raise RuntimeError("no pipeline steps registered")
        return f"{len(available)} step(s) available", {}

    def query_engine() -> tuple[str, dict[str, Any]]:
        from gdap.storage.query import DuckDBEngine

        with DuckDBEngine() as engine:
            value = engine.sql_unguarded("SELECT 42 AS answer").item()
        if value != 42:  # pragma: no cover - would mean a broken engine
            raise RuntimeError("query engine returned an unexpected result")
        return "DuckDB responding", {}

    def ai_runtime() -> tuple[str, dict[str, Any]]:
        from gdap.ai.providers import build_provider

        provider = build_provider(settings, platform.secrets)
        mode = "deterministic" if provider.name == "heuristic" else "llm"
        return f"{provider.name} provider ({mode})", {
            "provider": provider.name,
            "model": settings.ai.model if provider.name != "heuristic" else None,
        }

    def scheduler() -> tuple[str, dict[str, Any]]:
        from gdap.worker.scheduler import Scheduler

        upcoming = Scheduler(platform).upcoming(limit=5)
        return (
            f"{len(upcoming)} scheduled pipeline(s)" if upcoming else "no schedules configured",
            {"upcoming": upcoming},
        )

    checks.append(_timed("database", database))
    checks.append(_timed("filesystem", filesystem))
    checks.append(_timed("connectors", connectors))
    checks.append(_timed("pipeline_steps", steps))

    if deep:
        checks.append(_timed("query_engine", query_engine))
        checks.append(_timed("ai_runtime", ai_runtime))
        checks.append(_timed("scheduler", scheduler))
        checks.append(_timed("jobs", lambda: _job_stats(platform)))

    healthy = all(check.ok for check in checks)
    if not healthy:
        log.warning(
            "health_degraded",
            failed=[check.component for check in checks if not check.ok],
        )
    return SystemHealth(
        ok=healthy,
        version=__version__,
        environment=settings.environment,
        checks=checks,
    )


def _job_stats(platform: Platform) -> tuple[str, dict[str, Any]]:
    from sqlalchemy import func, select

    from gdap.storage import models as m

    with platform.db.session() as session:
        rows = session.execute(select(m.Job.state, func.count()).group_by(m.Job.state)).all()
    counts = {state: int(count) for state, count in rows}
    stuck = counts.get("RUNNING", 0)
    return (
        f"{sum(counts.values())} job(s) recorded" + (f", {stuck} running" if stuck else ""),
        {"by_state": counts},
    )
