"""Job service: the state machine around a pipeline run (§20).

``PENDING → RUNNING → SUCCESS | FAILED | RETRYING | AWAITING_APPROVAL | CANCELLED``

The executor knows how to run steps; this service knows what a *run* means: attempts, retries with
backoff, approval parking, cancellation, alerting on failure, and the audit/lineage trail that
makes a run explainable weeks later.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from typing import Any

from gdap.core.contracts import JobResult, PipelineSpec, StepResult
from gdap.core.enums import JobState, Permission, Severity, StepState, TriggerType
from gdap.core.errors import ConflictError, NotFoundError
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger, log_context, new_trace_id
from gdap.observability.metrics import METRICS
from gdap.pipelines.executor import PipelineExecutor
from gdap.security.rbac import require
from gdap.storage import models as m
from gdap.storage.repositories import ApprovalRepository, JobRepository, PipelineRepository

log = get_logger(__name__)


class JobService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = JobRepository(context.session, context.org_id)
        self.pipelines = PipelineRepository(context.session, context.org_id)
        self.approvals = ApprovalRepository(context.session, context.org_id)

    # ------------------------------------------------------------------ creation
    def create(
        self,
        *,
        pipeline: m.Pipeline | None,
        spec: PipelineSpec,
        params: dict[str, Any] | None = None,
        trigger: TriggerType = TriggerType.MANUAL,
        priority: int = 0,
        scheduled_for: datetime | None = None,
    ) -> m.Job:
        require(self.context.principal, Permission.PIPELINE_RUN, resource="job")
        job = self.repo.create(
            pipeline_id=pipeline.id if pipeline else None,
            pipeline_name=spec.name,
            pipeline_version=spec.version,
            spec=spec.model_dump(mode="json", by_alias=True),
            params=params or {},
            trigger=trigger.value,
            state=JobState.PENDING.value,
            priority=priority,
            max_attempts=spec.retry.max_attempts,
            scheduled_for=scheduled_for or datetime.now(UTC),
            created_by=self.context.principal.user_id,
            trace_id=new_trace_id(),
        )
        if pipeline:
            self.context.lineage.record(
                upstream_type="pipeline",
                upstream_id=pipeline.id,
                downstream_type="job",
                downstream_id=job.id,
                operation="trigger",
                job_id=job.id,
            )
        self.context.audit.record(
            self.context.principal,
            "job.create",
            "job",
            job.id,
            details={"pipeline": spec.name, "trigger": trigger.value, "params": params or {}},
        )
        METRICS.increment("jobs_created_total", pipeline=spec.name, trigger=trigger.value)
        log.info("job_created", job_id=job.id, pipeline=spec.name, trigger=trigger.value)
        return job

    # ------------------------------------------------------------------ execution
    def execute(self, job: m.Job | str, *, dry_run: bool = False) -> JobResult:
        """Run a job to completion in this process. Workers call this after leasing."""
        row = job if isinstance(job, m.Job) else self.repo.get_or_raise(job)
        if JobState(row.state).is_terminal:
            raise ConflictError(
                f"job {row.id} is already {row.state}", details={"state": row.state}
            )

        spec = PipelineSpec.model_validate(row.spec)
        if row.state != JobState.RUNNING.value:
            # An inline run owns its attempt counter. The worker path already incremented it while
            # leasing the job, so a job that arrives here already RUNNING must not be counted twice
            # — otherwise a retrying job would never exhaust max_attempts.
            row.attempt += 1
            self.repo.transition(row, JobState.RUNNING, worker_id=row.worker_id or "inline")
        attempt = max(row.attempt, 1)
        approved = set((row.approval_request or {}).get("approved_steps", []))
        superseded = self.repo.clear_steps(row.id, attempt)
        if superseded:
            log.info("job_steps_superseded", job_id=row.id, attempt=attempt, rows=superseded)
        step_index = {"value": 0}

        def persist_step(result: StepResult) -> None:
            self.repo.add_step(
                row.id,
                index=step_index["value"],
                attempt=attempt,
                step_id=result.step_id,
                uses=result.uses,
                state=result.state.value,
                rows_in=result.rows_in,
                rows_out=result.rows_out,
                metrics={k: v for k, v in result.metrics.items() if _serialisable(v)},
                artifacts=result.artifacts,
                error=result.error,
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
            step_index["value"] += 1

        def cancelled() -> bool:
            self.context.session.refresh(row, ["state"])
            return row.state == JobState.CANCELLED.value

        executor = PipelineExecutor(on_step=persist_step, is_cancelled=cancelled)
        with log_context(trace_id=row.trace_id, job_id=row.id, org_id=self.context.org_id):
            result = executor.execute(
                spec,
                self.context,
                job_id=row.id,
                params=row.params or {},
                approved_steps=approved,
                dry_run=dry_run,
                attempt=attempt,
            )
        self._finalise(row, spec, result)
        return result

    def _finalise(self, row: m.Job, spec: PipelineSpec, result: JobResult) -> None:
        payload = result.model_dump(mode="json")
        metrics = {k: v for k, v in result.metrics.items() if _serialisable(v)}

        if result.state is JobState.SUCCESS:
            self.repo.transition(row, JobState.SUCCESS, result=payload, metrics=metrics, error=None)
            self._record_outputs(row, result)

        elif result.state is JobState.AWAITING_APPROVAL:
            blocked = [step for step in result.steps if step.state is StepState.BLOCKED]
            self.repo.transition(
                row,
                JobState.AWAITING_APPROVAL,
                result=payload,
                metrics=metrics,
                error=result.error,
                approval_request={
                    "steps": [step.step_id for step in blocked],
                    "reason": result.error,
                    "requested_at": datetime.now(UTC).isoformat(),
                    "approved_steps": list((row.approval_request or {}).get("approved_steps", [])),
                },
            )
            self.approvals.create(
                job_id=row.id,
                kind="pipeline_step",
                payload={"steps": [step.step_id for step in blocked], "pipeline": spec.name},
                status="pending",
                requested_by=row.created_by or self.context.principal.user_id,
            )
            self.context.alerts.raise_alert(
                rule="job_awaiting_approval",
                severity=Severity.INFO,
                title=f"Pipeline '{spec.name}' is waiting for approval",
                message=result.error or "a step requires a human decision",
                payload={"job_id": row.id, "steps": [step.step_id for step in blocked]},
                dedupe_key=f"approval:{row.id}",
            )

        elif result.state is JobState.CANCELLED:
            self.repo.transition(row, JobState.CANCELLED, result=payload, metrics=metrics)

        else:  # FAILED
            if row.attempt < row.max_attempts:
                delay = min(
                    spec.retry.backoff_seconds
                    * (spec.retry.backoff_multiplier ** (row.attempt - 1)),
                    spec.retry.max_backoff_seconds,
                )
                self.repo.transition(
                    row,
                    JobState.RETRYING,
                    result=payload,
                    metrics=metrics,
                    error=result.error,
                    error_code=result.error_code,
                    scheduled_for=datetime.now(UTC) + timedelta(seconds=delay),
                    lease_until=None,
                    worker_id=None,
                )
                log.warning(
                    "job_retry_scheduled",
                    job_id=row.id,
                    attempt=row.attempt,
                    max_attempts=row.max_attempts,
                    retry_in_seconds=round(delay, 1),
                )
                METRICS.increment("job_retries_total", pipeline=spec.name)
            else:
                self.repo.transition(
                    row,
                    JobState.FAILED,
                    result=payload,
                    metrics=metrics,
                    error=result.error,
                    error_code=result.error_code,
                )
                self.context.alerts.raise_alert(
                    rule="pipeline_failure",
                    severity=Severity.CRITICAL,
                    title=f"Pipeline '{spec.name}' failed after {row.attempt} attempt(s)",
                    message=result.error or "unknown error",
                    payload={
                        "job_id": row.id,
                        "pipeline": spec.name,
                        "error_code": result.error_code,
                        "failed_steps": [
                            step.step_id for step in result.steps if step.state is StepState.FAILED
                        ],
                    },
                    dedupe_key=f"pipeline_failure:{spec.name}",
                )

        if row.pipeline_id:
            pipeline = self.pipelines.get(row.pipeline_id)
            if pipeline:
                pipeline.last_state = row.state
                pipeline.last_run_at = datetime.now(UTC)
        self.context.session.flush()

        METRICS.increment("jobs_finished_total", pipeline=spec.name, state=row.state)
        self.context.audit.record(
            self.context.principal,
            "job.finish",
            "job",
            row.id,
            result="success" if result.state is JobState.SUCCESS else "error",
            details={
                "pipeline": spec.name,
                "state": row.state,
                "attempt": row.attempt,
                "duration_seconds": metrics.get("duration_seconds"),
                "error": result.error,
            },
        )

    def _record_outputs(self, row: m.Job, result: JobResult) -> None:
        from gdap.storage.repositories import DatasetRepository

        datasets = DatasetRepository(self.context.session, self.context.org_id)
        for name in result.metrics.get("datasets_written", []) or []:
            dataset = datasets.by_name(str(name))
            if dataset is None:
                continue
            version = datasets.latest_version(dataset.id)
            if version:
                self.context.lineage.record(
                    upstream_type="job",
                    upstream_id=row.id,
                    downstream_type="dataset_version",
                    downstream_id=version.id,
                    operation="pipeline_write",
                    job_id=row.id,
                )

    # ------------------------------------------------------------------ control
    def cancel(self, job_id: str, *, reason: str | None = None) -> m.Job:
        require(self.context.principal, Permission.JOB_WRITE, resource="job")
        row = self.repo.get_or_raise(job_id)
        if JobState(row.state).is_terminal:
            raise ConflictError(f"job {job_id} is already {row.state}")
        self.repo.transition(row, JobState.CANCELLED, error=reason or "cancelled by user")
        self.context.audit.record(
            self.context.principal, "job.cancel", "job", row.id, details={"reason": reason}
        )
        return row

    def approve(
        self, job_id: str, *, steps: list[str] | None = None, note: str | None = None
    ) -> m.Job:
        """Approve blocked steps and re-queue the job (§38)."""
        require(self.context.principal, Permission.JOB_WRITE, resource="job")
        row = self.repo.get_or_raise(job_id)
        if row.state != JobState.AWAITING_APPROVAL.value:
            raise ConflictError(
                f"job {job_id} is not awaiting approval", details={"state": row.state}
            )
        request = dict(row.approval_request or {})
        pending = list(request.get("steps", []))
        approved = set(request.get("approved_steps", [])) | set(steps or pending)
        request["approved_steps"] = sorted(approved)
        request["decided_by"] = self.context.principal.user_id
        request["decided_at"] = datetime.now(UTC).isoformat()
        request["note"] = note

        row.approval_request = request
        row.attempt = max(row.attempt - 1, 0)  # the approved retry is not a failure attempt
        self.repo.transition(
            row,
            JobState.PENDING,
            scheduled_for=datetime.now(UTC),
            error=None,
            lease_until=None,
            worker_id=None,
        )
        approval_row = self.approvals.pending_for_job(row.id)
        if approval_row:
            approval_row.status = "approved"
            approval_row.decided_by = self.context.principal.user_id
            approval_row.decided_at = datetime.now(UTC)
            approval_row.reason = note
        self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "job.approve",
            "job",
            row.id,
            details={"approved_steps": sorted(approved), "note": note},
        )
        log.info("job_approved", job_id=row.id, steps=sorted(approved))
        return row

    def reject(self, job_id: str, *, reason: str) -> m.Job:
        require(self.context.principal, Permission.JOB_WRITE, resource="job")
        row = self.repo.get_or_raise(job_id)
        approval_row = self.approvals.pending_for_job(row.id)
        if approval_row:
            approval_row.status = "rejected"
            approval_row.decided_by = self.context.principal.user_id
            approval_row.decided_at = datetime.now(UTC)
            approval_row.reason = reason
        self.repo.transition(row, JobState.CANCELLED, error=f"approval rejected: {reason}")
        self.context.audit.record(
            self.context.principal, "job.reject", "job", row.id, details={"reason": reason}
        )
        return row

    def retry(self, job_id: str) -> m.Job:
        """Re-queue a failed job, resetting its attempt counter."""
        require(self.context.principal, Permission.JOB_WRITE, resource="job")
        row = self.repo.get_or_raise(job_id)
        if row.state not in {JobState.FAILED.value, JobState.CANCELLED.value}:
            raise ConflictError(
                f"only failed or cancelled jobs can be retried (job is {row.state})"
            )
        row.state = JobState.PENDING.value
        row.attempt = 0
        row.error = None
        row.error_code = None
        row.finished_at = None
        row.lease_until = None
        row.worker_id = None
        row.scheduled_for = datetime.now(UTC)
        self.context.session.flush()
        self.context.audit.record(self.context.principal, "job.retry", "job", row.id)
        return row

    # ------------------------------------------------------------------ reading
    def get(self, job_id: str) -> m.Job:
        require(self.context.principal, Permission.JOB_READ, resource="job")
        row = self.repo.get(job_id)
        if row is None:
            raise NotFoundError(f"job '{job_id}' not found")
        return row

    def list(
        self, *, limit: int = 50, state: str | None = None, pipeline: str | None = None
    ) -> builtins.list[m.Job]:
        require(self.context.principal, Permission.JOB_READ, resource="job")
        return self.repo.list(limit=limit, state=state, pipeline_name=pipeline)

    def steps(self, job_id: str, *, attempt: int | None = None) -> builtins.list[m.JobStep]:
        """Steps of a run. By default the *latest* attempt only — that is what "what happened"
        means to an operator; pass ``attempt`` to inspect an earlier one."""
        self.get(job_id)
        rows = self.repo.steps(job_id)
        if not rows:
            return []
        target = attempt if attempt is not None else max(step.attempt for step in rows)
        return [step for step in rows if step.attempt == target]

    def claim_next(self, worker_id: str, *, lease_seconds: int) -> m.Job | None:
        return self.repo.claim_next(worker_id, lease_seconds=lease_seconds)

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int) -> bool:
        return self.repo.heartbeat(job_id, worker_id, lease_seconds=lease_seconds)

    def to_dict(self, row: m.Job, *, include_steps: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row.id,
            "pipeline": row.pipeline_name,
            "pipeline_id": row.pipeline_id,
            "pipeline_version": row.pipeline_version,
            "state": row.state,
            "trigger": row.trigger,
            "attempt": row.attempt,
            "max_attempts": row.max_attempts,
            "params": row.params or {},
            "metrics": row.metrics or {},
            "error": row.error,
            "error_code": row.error_code,
            "approval_request": row.approval_request or {},
            "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "created_at": row.created_at.isoformat(),
            "created_by": row.created_by,
            "trace_id": row.trace_id,
            "artifacts": (row.result or {}).get("artifacts", []),
            "insights": (row.result or {}).get("insights", []),
        }
        if include_steps:
            payload["steps"] = [
                {
                    "index": step.index,
                    "attempt": step.attempt,
                    "step_id": step.step_id,
                    "uses": step.uses,
                    "state": step.state,
                    "rows_in": step.rows_in,
                    "rows_out": step.rows_out,
                    "metrics": step.metrics or {},
                    "artifacts": step.artifacts or [],
                    "error": step.error,
                    "started_at": step.started_at.isoformat() if step.started_at else None,
                    "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                }
                for step in self.repo.steps(row.id)
            ]
        return payload


def _serialisable(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | list | dict | type(None))
