"""Pipeline service: create, version, schedule and trigger pipelines."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any

from gdap.core.contracts import PipelineSpec
from gdap.core.enums import Permission, TriggerType
from gdap.core.errors import ConflictError
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.pipelines.schedule import describe as describe_schedule
from gdap.pipelines.schedule import next_run
from gdap.pipelines.spec import dump_spec, parse_spec, validate_steps
from gdap.security.rbac import require
from gdap.storage import models as m
from gdap.storage.repositories import PipelineRepository

log = get_logger(__name__)


class PipelineService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = PipelineRepository(context.session, context.org_id)

    # ------------------------------------------------------------------ authoring
    def create(self, payload: PipelineSpec | dict[str, Any] | str) -> m.Pipeline:
        require(self.context.principal, Permission.PIPELINE_WRITE, resource="pipeline")
        spec = payload if isinstance(payload, PipelineSpec) else parse_spec(payload)
        validate_steps(spec)
        if self.repo.by_name(spec.name):
            raise ConflictError(
                f"pipeline '{spec.name}' already exists",
                details={"hint": "use update() to publish a new version"},
            )

        row = self.repo.create(
            name=spec.name,
            description=spec.description,
            spec=spec.model_dump(mode="json", by_alias=True),
            version=spec.version,
            fingerprint=spec.fingerprint(),
            enabled=True,
            schedule_cron=spec.schedule.cron if spec.schedule else None,
            schedule_timezone=spec.schedule.timezone if spec.schedule else "UTC",
            next_run_at=next_run(spec.schedule) if spec.schedule else None,
            owner=spec.owner or self.context.principal.email or self.context.principal.user_id,
            tags=spec.tags,
        )
        self.repo.add_version(row, row.spec, self.context.principal.user_id)
        self.context.audit.record(
            self.context.principal,
            "pipeline.create",
            "pipeline",
            row.id,
            details={
                "name": spec.name,
                "steps": [step.uses for step in spec.steps],
                "schedule": describe_schedule(spec.schedule) if spec.schedule else None,
            },
        )
        log.info("pipeline_created", name=spec.name, steps=len(spec.steps))
        return row

    def update(self, reference: str, payload: PipelineSpec | dict[str, Any] | str) -> m.Pipeline:
        """Publish a new version. Old versions stay readable — runs are reproducible (§42)."""
        require(self.context.principal, Permission.PIPELINE_WRITE, resource="pipeline")
        row = self.repo.require_by_name_or_id(reference)
        spec = payload if isinstance(payload, PipelineSpec) else parse_spec(payload)
        validate_steps(spec)

        row.version += 1
        spec = spec.model_copy(update={"version": row.version, "name": row.name})
        row.spec = spec.model_dump(mode="json", by_alias=True)
        row.fingerprint = spec.fingerprint()
        row.description = spec.description
        row.tags = spec.tags
        row.schedule_cron = spec.schedule.cron if spec.schedule else None
        row.schedule_timezone = spec.schedule.timezone if spec.schedule else "UTC"
        row.next_run_at = next_run(spec.schedule) if spec.schedule else None
        self.context.session.flush()
        self.repo.add_version(row, row.spec, self.context.principal.user_id)
        self.context.audit.record(
            self.context.principal,
            "pipeline.update",
            "pipeline",
            row.id,
            details={"version": row.version, "fingerprint": row.fingerprint},
        )
        return row

    def delete(self, reference: str) -> None:
        require(self.context.principal, Permission.PIPELINE_WRITE, resource="pipeline")
        row = self.repo.require_by_name_or_id(reference)
        self.repo.delete(row.id)
        self.context.audit.record(
            self.context.principal,
            "pipeline.delete",
            "pipeline",
            row.id,
            details={"name": row.name},
        )

    def set_enabled(self, reference: str, enabled: bool) -> m.Pipeline:
        require(self.context.principal, Permission.PIPELINE_WRITE, resource="pipeline")
        row = self.repo.require_by_name_or_id(reference)
        row.enabled = enabled
        spec = self.spec_of(row)
        row.next_run_at = next_run(spec.schedule) if (enabled and spec.schedule) else None
        self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "pipeline.enable" if enabled else "pipeline.disable",
            "pipeline",
            row.id,
        )
        return row

    # ------------------------------------------------------------------ reading
    def list(self, *, limit: int = 100, offset: int = 0) -> builtins.list[m.Pipeline]:
        require(self.context.principal, Permission.PIPELINE_READ, resource="pipeline")
        return self.repo.list(limit=limit, offset=offset)

    def get(self, reference: str) -> m.Pipeline:
        require(self.context.principal, Permission.PIPELINE_READ, resource="pipeline")
        return self.repo.require_by_name_or_id(reference)

    def spec_of(self, row: m.Pipeline) -> PipelineSpec:
        return PipelineSpec.model_validate(row.spec)

    def as_yaml(self, reference: str) -> str:
        return dump_spec(self.spec_of(self.get(reference)))

    def versions(self, reference: str, *, limit: int = 20) -> builtins.list[m.PipelineVersion]:
        row = self.get(reference)
        from sqlalchemy import select

        from gdap.storage.models import PipelineVersion

        statement = (
            select(PipelineVersion)
            .where(PipelineVersion.pipeline_id == row.id)
            .order_by(PipelineVersion.version.desc())
            .limit(limit)
        )
        return list(self.context.session.execute(statement).scalars().all())

    # ------------------------------------------------------------------ execution
    def run(
        self,
        reference: str,
        *,
        params: dict[str, Any] | None = None,
        trigger: TriggerType = TriggerType.MANUAL,
        priority: int = 0,
        scheduled_for: datetime | None = None,
    ) -> m.Job:
        """Enqueue a run. Execution happens in a worker (or inline via ``JobService.execute``)."""
        require(self.context.principal, Permission.PIPELINE_RUN, resource="pipeline")
        row = self.repo.require_by_name_or_id(reference)
        if not row.enabled:
            raise ConflictError(f"pipeline '{row.name}' is disabled")

        decision = self.context.policy.decide(self.context.principal, "pipeline.run")
        decision.enforce()

        job = self.context.jobs.create(
            pipeline=row,
            spec=self.spec_of(row),
            params=params or {},
            trigger=trigger,
            priority=priority,
            scheduled_for=scheduled_for,
        )
        row.last_run_at = datetime.now(UTC)
        self.context.session.flush()
        return job

    def run_adhoc(
        self,
        payload: PipelineSpec | dict[str, Any] | str,
        *,
        params: dict[str, Any] | None = None,
    ) -> m.Job:
        """Run a spec that is not stored — used by the AI planner's 'review then run' flow."""
        require(self.context.principal, Permission.PIPELINE_RUN, resource="pipeline")
        spec = payload if isinstance(payload, PipelineSpec) else parse_spec(payload)
        validate_steps(spec)
        return self.context.jobs.create(
            pipeline=None,
            spec=spec,
            params=params or {},
            trigger=TriggerType.API,
        )

    def due(self, *, now: datetime | None = None, limit: int = 50) -> builtins.list[m.Pipeline]:
        return self.repo.due(now=now, limit=limit)

    def reschedule(self, row: m.Pipeline, *, after: datetime | None = None) -> datetime | None:
        """Advance ``next_run_at`` past ``after`` — called by the scheduler once a run is queued."""
        spec = self.spec_of(row)
        if not spec.schedule or not row.enabled:
            row.next_run_at = None
        else:
            row.next_run_at = next_run(spec.schedule, after=after or datetime.now(UTC))
        self.context.session.flush()
        return row.next_run_at

    # ------------------------------------------------------------------ presentation
    @staticmethod
    def to_dict(row: m.Pipeline) -> dict[str, Any]:
        spec = row.spec or {}
        return {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "version": row.version,
            "fingerprint": row.fingerprint,
            "enabled": row.enabled,
            "steps": [step.get("uses") for step in spec.get("steps", [])],
            "step_count": len(spec.get("steps", [])),
            "schedule": spec.get("schedule"),
            "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "last_state": row.last_state,
            "owner": row.owner,
            "tags": row.tags or [],
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
