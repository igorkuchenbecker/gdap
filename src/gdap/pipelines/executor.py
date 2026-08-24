"""Pipeline executor (§10, §20).

Runs a :class:`PipelineSpec` step by step against a :class:`ServiceContext`, producing a
:class:`JobResult`. Responsibilities kept deliberately here (and not in the steps):

* frame plumbing between steps (``input`` / ``output`` names)
* ``when:`` guards evaluated over run parameters and accumulated metrics
* approval gating — a step that needs a human raises, and the job parks in AWAITING_APPROVAL
* error containment — ``continue_on_error`` per step, ``on_failure`` for the pipeline
* per-step timing, metrics, artifacts and structured logs
* the pipeline-level quality gate

Retries are *not* here: they belong to the job runner, because retrying means re-leasing a job,
not re-running a loop (see :mod:`gdap.worker.runner`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from gdap.core.contracts import (
    JobResult,
    PipelineSpec,
    StepResult,
    StepSpec,
)
from gdap.core.enums import ApprovalMode, JobState, StepState
from gdap.core.errors import (
    ApprovalRequiredError,
    GdapError,
    JobCancelledError,
    QualityGateError,
)
from gdap.observability.logging import get_logger, log_context
from gdap.observability.metrics import METRICS
from gdap.pipelines.expressions import evaluate_scalar
from gdap.pipelines.spec import interpolate, runtime_params
from gdap.pipelines.steps import StepContext, StepOutcome, get_step

log = get_logger(__name__)

StepListener = Callable[[StepResult], None]
CancellationCheck = Callable[[], bool]


class PipelineExecutor:
    def __init__(
        self,
        *,
        on_step: StepListener | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> None:
        self._on_step = on_step
        self._is_cancelled = is_cancelled or (lambda: False)

    def execute(
        self,
        spec: PipelineSpec,
        services: Any,
        *,
        job_id: str | None = None,
        params: dict[str, Any] | None = None,
        approved_steps: set[str] | None = None,
        dry_run: bool = False,
        attempt: int = 1,
    ) -> JobResult:
        started = datetime.now(UTC)
        resolved_params = runtime_params(spec, params)
        context = StepContext(
            services=services,
            params=resolved_params,
            job_id=job_id,
            pipeline=spec.name,
            approved_steps=set(approved_steps or set()),
            dry_run=dry_run,
        )
        results: list[StepResult] = []
        state = JobState.SUCCESS
        error: str | None = None
        error_code: str | None = None

        with log_context(pipeline=spec.name, job_id=job_id, attempt=attempt):
            log.info(
                "pipeline_started",
                steps=len(spec.steps),
                dry_run=dry_run,
                params={k: v for k, v in resolved_params.items() if not k.startswith("_")},
            )
            for index, step in enumerate(spec.steps):
                step_id = step.id or f"{step.uses}#{index + 1}"

                if self._is_cancelled():
                    results.append(_skipped(step_id, step, "cancelled before execution"))
                    state = JobState.CANCELLED
                    error, error_code = "job cancelled", JobCancelledError.code
                    break

                if not self._should_run(step, context):
                    result = _skipped(step_id, step, f"condition not met: {step.when}")
                    results.append(result)
                    self._emit(result)
                    continue

                result = self._run_step(step_id, step, context, index)
                results.append(result)
                self._emit(result)

                if result.state is StepState.BLOCKED:
                    state = JobState.AWAITING_APPROVAL
                    error, error_code = result.error, result.error_code
                    break
                if result.state is StepState.FAILED:
                    if step.continue_on_error or spec.on_failure == "continue":
                        log.warning("step_failed_continuing", step=step_id, error=result.error)
                        continue
                    state = JobState.FAILED
                    error, error_code = result.error, result.error_code
                    break

            if state is JobState.SUCCESS and spec.quality_gate is not None:
                score = context.metrics.get("quality_score")
                if score is not None and float(score) < float(spec.quality_gate):
                    state = JobState.FAILED
                    gate_error = QualityGateError(
                        f"pipeline quality gate failed: {float(score):.1f} < {spec.quality_gate:.1f}",
                        details={"score": score, "threshold": spec.quality_gate},
                    )
                    error, error_code = gate_error.message, gate_error.code
                    log.error(
                        "pipeline_quality_gate_failed", score=score, threshold=spec.quality_gate
                    )

            finished = datetime.now(UTC)
            duration = (finished - started).total_seconds()
            METRICS.increment("pipeline_runs_total", pipeline=spec.name, state=state.value)
            METRICS.observe("pipeline_duration_s", duration, pipeline=spec.name)
            log.info(
                "pipeline_finished",
                state=state.value,
                duration_s=round(duration, 3),
                steps_run=sum(1 for r in results if r.state is StepState.SUCCESS),
                steps_failed=sum(1 for r in results if r.state is StepState.FAILED),
                artifacts=len(context.artifacts),
            )

        return JobResult(
            job_id=job_id or "ad-hoc",
            pipeline=spec.name,
            pipeline_version=spec.version,
            state=state,
            started_at=started,
            finished_at=finished,
            steps=results,
            metrics={
                **context.metrics,
                "duration_seconds": round(duration, 3),
                "datasets_written": context.datasets_written,
            },
            artifacts=context.artifacts,
            insights=context.insights,
            error=error,
            error_code=error_code,
            attempt=attempt,
        )

    # ------------------------------------------------------------------ internals
    def _run_step(
        self, step_id: str, step: StepSpec, context: StepContext, index: int
    ) -> StepResult:
        definition = get_step(step.uses)
        approval = step.approval or definition.approval
        started = datetime.now(UTC)
        rows_in = context.frames.get(step.input or context.current or "", None)
        rows_in_count = rows_in.height if rows_in is not None else None

        with log_context(step=step_id, uses=step.uses):
            if approval is ApprovalMode.BLOCKED:
                return _blocked(step_id, step, started, "step is blocked by policy")
            if approval is ApprovalMode.REQUIRES_APPROVAL and step_id not in context.approved_steps:
                log.warning("step_requires_approval", approval=approval.value)
                return _blocked(
                    step_id,
                    step,
                    started,
                    f"step '{step_id}' requires human approval before it can run",
                )

            resolved = step.model_copy(update={"with_": interpolate(step.with_, context.params)})
            clock = time.perf_counter()
            try:
                outcome: StepOutcome = definition.handler(context, resolved)
            except GdapError as exc:
                log.error("step_failed", error=exc.message, code=exc.code)
                METRICS.increment("pipeline_steps_total", step=step.uses, state="failed")
                return StepResult(
                    step_id=step_id,
                    uses=step.uses,
                    state=StepState.FAILED,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    rows_in=rows_in_count,
                    error=exc.message,
                    error_code=exc.code,
                    metrics={"details": exc.details} if exc.details else {},
                )
            except Exception as exc:
                log.exception("step_crashed", error=str(exc))
                METRICS.increment("pipeline_steps_total", step=step.uses, state="crashed")
                return StepResult(
                    step_id=step_id,
                    uses=step.uses,
                    state=StepState.FAILED,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    rows_in=rows_in_count,
                    error=f"{type(exc).__name__}: {exc}",
                    error_code="GDAP-5002",
                )

            elapsed_ms = (time.perf_counter() - clock) * 1000
            METRICS.observe("pipeline_step_ms", elapsed_ms, step=step.uses)
            METRICS.increment("pipeline_steps_total", step=step.uses, state="success")

            if outcome.requires_approval and step_id not in context.approved_steps:
                return _blocked(step_id, step, started, outcome.requires_approval)

            for name, frame in outcome.outputs.items():
                context.frames[name] = frame
            if outcome.frame is not None and step.output:
                context.publish(step.output, outcome.frame)
            context.metrics.update(
                {
                    key: value
                    for key, value in outcome.metrics.items()
                    if isinstance(value, int | float | str | bool | list)
                }
            )
            context.artifacts.extend(outcome.artifacts)

            if elapsed_ms > context.services.settings.observability.slow_step_ms:
                log.warning("slow_step", duration_ms=round(elapsed_ms, 1))
            log.info("step_completed", duration_ms=round(elapsed_ms, 1), message=outcome.message)

            return StepResult(
                step_id=step_id,
                uses=step.uses,
                state=StepState.SUCCESS,
                started_at=started,
                finished_at=datetime.now(UTC),
                rows_in=rows_in_count,
                rows_out=outcome.frame.height if outcome.frame is not None else None,
                metrics={"message": outcome.message, **outcome.metrics},
                artifacts=outcome.artifacts,
            )

    def _should_run(self, step: StepSpec, context: StepContext) -> bool:
        if not step.when:
            return True
        variables = {**context.params, **context.metrics}
        try:
            return bool(evaluate_scalar(step.when, variables))
        except GdapError as exc:
            log.warning("condition_not_evaluable", condition=step.when, error=exc.message)
            return False

    def _emit(self, result: StepResult) -> None:
        if self._on_step is not None:
            try:
                self._on_step(result)
            except Exception as exc:  # listener problems must not fail the run
                log.error("step_listener_failed", error=str(exc))


def _skipped(step_id: str, step: StepSpec, reason: str) -> StepResult:
    now = datetime.now(UTC)
    return StepResult(
        step_id=step_id,
        uses=step.uses,
        state=StepState.SKIPPED,
        started_at=now,
        finished_at=now,
        metrics={"reason": reason},
    )


def _blocked(step_id: str, step: StepSpec, started: datetime, reason: str) -> StepResult:
    return StepResult(
        step_id=step_id,
        uses=step.uses,
        state=StepState.BLOCKED,
        started_at=started,
        finished_at=datetime.now(UTC),
        error=reason,
        error_code=ApprovalRequiredError.code,
    )
