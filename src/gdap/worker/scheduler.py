"""Scheduler (§21).

Every tick: find pipelines whose ``next_run_at`` has passed, queue one job each, and advance the
schedule. Advancing *before* the job runs is deliberate — a slow run must not silently skip the
next window, and duplicate queueing is prevented by the schedule row itself.

Dependencies between pipelines (``depends_on``) are honoured here: a pipeline only fires once its
upstreams have a successful run in the current window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gdap.core.container import Platform
from gdap.core.contracts import Principal
from gdap.core.enums import JobState, TriggerType
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.storage import models as m
from gdap.storage.repositories import JobRepository, PipelineRepository

log = get_logger(__name__)


class Scheduler:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def tick(self, *, now: datetime | None = None, limit: int = 50) -> int:
        """Queue everything that is due. Returns how many jobs were created."""
        moment = now or datetime.now(UTC)
        queued = 0

        with self.platform.db.session() as session:
            due = PipelineRepository(session, "*").due(now=moment, limit=limit)
            for pipeline in due:
                principal = Principal.system(pipeline.org_id, "scheduler")
                context = self.platform.context(session, principal)
                try:
                    if not self._dependencies_met(session, pipeline, moment):
                        log.info(
                            "schedule_waiting_on_dependencies",
                            pipeline=pipeline.name,
                            depends_on=(pipeline.spec or {}).get("depends_on", []),
                        )
                        continue
                    spec = context.pipelines.spec_of(pipeline)
                    job = context.jobs.create(
                        pipeline=pipeline,
                        spec=spec,
                        params=dict(spec.params or {}),
                        trigger=TriggerType.SCHEDULE,
                    )
                    context.pipelines.reschedule(pipeline, after=moment)
                    queued += 1
                    METRICS.increment("scheduler_jobs_total", pipeline=pipeline.name)
                    log.info(
                        "schedule_fired",
                        pipeline=pipeline.name,
                        job_id=job.id,
                        next_run_at=pipeline.next_run_at.isoformat()
                        if pipeline.next_run_at
                        else None,
                    )
                except Exception as exc:
                    log.error("schedule_failed", pipeline=pipeline.name, error=str(exc))
                    # push the schedule forward so one broken pipeline cannot spin the loop
                    pipeline.next_run_at = moment + timedelta(minutes=5)
        return queued

    def _dependencies_met(self, session: Any, pipeline: m.Pipeline, moment: datetime) -> bool:
        depends_on = list((pipeline.spec or {}).get("depends_on", []) or [])
        if not depends_on:
            return True
        jobs = JobRepository(session, pipeline.org_id)
        window_start = moment - timedelta(hours=24)
        for upstream in depends_on:
            recent = jobs.list(limit=5, pipeline_name=str(upstream), state=JobState.SUCCESS.value)
            fresh = [
                job
                for job in recent
                if job.finished_at
                and (
                    job.finished_at.replace(tzinfo=UTC)
                    if job.finished_at.tzinfo is None
                    else job.finished_at
                )
                >= window_start
            ]
            if not fresh:
                return False
        return True

    def upcoming(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """What is scheduled next, across tenants — the operator's view."""
        from sqlalchemy import select

        with self.platform.db.session() as session:
            statement = (
                select(m.Pipeline)
                .where(m.Pipeline.enabled.is_(True), m.Pipeline.next_run_at.is_not(None))
                .order_by(m.Pipeline.next_run_at.asc())
                .limit(limit)
            )
            rows = session.execute(statement).scalars().all()
            return [
                {
                    "pipeline": row.name,
                    "org_id": row.org_id,
                    "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                    "cron": row.schedule_cron,
                    "timezone": row.schedule_timezone,
                    "last_state": row.last_state,
                }
                for row in rows
            ]
