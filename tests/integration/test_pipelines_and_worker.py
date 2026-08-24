"""Pipeline execution, approval gates, retries, the worker and the scheduler."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gdap.core.contracts import SourceSpec
from gdap.core.enums import JobState, SourceType, StepState
from gdap.core.errors import PipelineSpecError
from gdap.core.services.context import ServiceContext
from gdap.pipelines.spec import load_spec, parse_spec
from gdap.worker import JobRunner, Scheduler, WorkerConfig

pytestmark = pytest.mark.integration


def _register(context: ServiceContext, demo_dir: Path) -> None:
    if not context.sources.repo.by_name("files"):
        context.sources.register(
            SourceSpec(
                name="files",
                type=SourceType.FILE,
                connector="file.csv",
                config={"path": str(demo_dir), "pattern": "*.csv"},
            )
        )


ANALYTICS_PIPELINE: dict[str, Any] = {
    "name": "sales_flow",
    "steps": [
        {
            "id": "ingest",
            "uses": "read.source",
            "with": {"source": "files", "object": "transactions.csv", "dataset": "tx"},
        },
        {"id": "clean", "uses": "clean.auto", "with": {"apply": "validated", "dataset": "tx"}},
        {
            "id": "revenue",
            "uses": "transform.calculate",
            "with": {"calculate": {"net": "revenue * (1 - discount_pct)"}},
        },
        {
            "id": "validate",
            "uses": "validate.expectations",
            "with": {"auto": True, "dataset": "tx"},
        },
        {
            "id": "monthly",
            "uses": "aggregate",
            "output": "monthly",
            "with": {"group_by": ["region"], "metrics": {"net": "sum(net)", "orders": "count"}},
        },
        {"id": "publish", "uses": "write.dataset", "with": {"dataset": "tx_by_region"}},
        {
            "id": "trend",
            "uses": "analyze.trend",
            "input": "tx",
            "with": {"metric": "net", "time_column": "order_date", "granularity": "month"},
        },
        {
            "id": "report",
            "uses": "report.generate",
            "with": {"title": "Test report", "formats": ["html"], "dataset": "tx_by_region"},
        },
    ],
}


def test_pipeline_runs_end_to_end(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    context.pipelines.create(ANALYTICS_PIPELINE)
    job = context.pipelines.run("sales_flow")
    result = context.jobs.execute(job)

    assert result.state is JobState.SUCCESS
    assert all(step.state is StepState.SUCCESS for step in result.steps)
    assert result.artifacts, "the report step must produce an artifact"
    assert result.insights, "analysis steps must contribute insights"
    assert context.datasets.get("tx_by_region").row_count > 0
    assert result.metrics["quality_score"] > 0


def test_pipeline_records_steps_and_lineage(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    context.pipelines.create(ANALYTICS_PIPELINE)
    job = context.pipelines.run("sales_flow")
    context.jobs.execute(job)

    steps = context.jobs.steps(job.id)
    assert [step.step_id for step in steps][:3] == ["ingest", "clean", "revenue"]
    assert all(step.attempt == 1 for step in steps)

    edges = context.lineage.for_job(job.id)
    assert any(edge["operation"].startswith("ingest") for edge in edges)


def test_quality_gate_stops_the_pipeline(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    spec = {
        "name": "strict_gate",
        "steps": [
            {
                "id": "ingest",
                "uses": "read.source",
                "with": {"source": "files", "object": "transactions.csv", "dataset": "tx2"},
            },
            {
                "id": "validate",
                "uses": "validate.expectations",
                "with": {"auto": True, "dataset": "tx2", "min_score": 99.9},
            },
        ],
    }
    context.pipelines.create(spec)
    result = context.jobs.execute(context.pipelines.run("strict_gate"))
    assert result.state is JobState.FAILED
    assert result.error_code == "GDAP-4300"


def test_approval_gate_parks_and_resumes(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    spec = {
        "name": "gated",
        "retry": {"max_attempts": 1},
        "steps": [
            {
                "id": "ingest",
                "uses": "read.source",
                "with": {"source": "files", "object": "products.csv", "dataset": "p"},
            },
            {
                "id": "danger",
                "uses": "clean.outliers",
                "approval": "REQUIRES_APPROVAL",
                "with": {"action": "clip"},
            },
            {"id": "publish", "uses": "write.dataset", "with": {"dataset": "p_clean"}},
        ],
    }
    context.pipelines.create(spec)
    job = context.pipelines.run("gated")
    first = context.jobs.execute(job)

    assert first.state is JobState.AWAITING_APPROVAL
    assert context.jobs.get(job.id).approval_request["steps"] == ["danger"]

    context.jobs.approve(job.id, note="reviewed")
    second = context.jobs.execute(context.jobs.get(job.id))
    assert second.state is JobState.SUCCESS
    assert [step.step_id for step in context.jobs.steps(job.id)] == ["ingest", "danger", "publish"]


def test_failure_retries_then_alerts(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    spec = {
        "name": "flaky",
        "retry": {"max_attempts": 2, "backoff_seconds": 0.01, "backoff_multiplier": 1},
        "steps": [
            {
                "id": "ingest",
                "uses": "read.source",
                "with": {"source": "files", "object": "regions.csv", "dataset": "r"},
            },
            {"id": "bad", "uses": "validate.schema", "with": {"columns": ["does_not_exist"]}},
        ],
    }
    context.pipelines.create(spec)
    job = context.pipelines.run("flaky")

    context.jobs.execute(job)
    assert context.jobs.get(job.id).state == JobState.RETRYING.value

    time.sleep(0.05)
    context.jobs.execute(context.jobs.get(job.id))
    assert context.jobs.get(job.id).state == JobState.FAILED.value
    assert any(alert.rule == "pipeline_failure" for alert in context.alerts.list())


def test_step_conditions_skip_without_failing(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    spec = {
        "name": "conditional",
        "params": {"threshold": 100},
        "steps": [
            {
                "id": "ingest",
                "uses": "read.source",
                "with": {"source": "files", "object": "regions.csv", "dataset": "r2"},
            },
            {"id": "skipped", "uses": "profile", "when": "threshold > 1000"},
            {"id": "executed", "uses": "profile", "when": "threshold > 10"},
        ],
    }
    context.pipelines.create(spec)
    result = context.jobs.execute(context.pipelines.run("conditional"))
    states = {step.step_id: step.state for step in result.steps}
    assert states["skipped"] is StepState.SKIPPED
    assert states["executed"] is StepState.SUCCESS


def test_parameter_interpolation(context: ServiceContext, demo_dir: Path) -> None:
    _register(context, demo_dir)
    spec = {
        "name": "parameterised",
        "params": {"target": "regions.csv", "dataset_name": "r3"},
        "steps": [
            {
                "id": "ingest",
                "uses": "read.source",
                "with": {
                    "source": "files",
                    "object": "${params.target}",
                    "dataset": "${params.dataset_name}",
                },
            },
        ],
    }
    context.pipelines.create(spec)
    result = context.jobs.execute(context.pipelines.run("parameterised"))
    assert result.state is JobState.SUCCESS
    assert context.datasets.get("r3").row_count == 4


def test_invalid_specs_are_rejected_at_creation() -> None:
    with pytest.raises(PipelineSpecError):
        parse_spec({"name": "broken", "steps": [{"uses": "does.not.exist"}]})
    with pytest.raises(PipelineSpecError):
        parse_spec({"name": "empty", "steps": []})


def test_reference_pipeline_file_is_valid() -> None:
    spec = load_spec(Path("examples/pipelines/sales_daily.yaml"))
    assert spec.name == "sales_daily"
    assert spec.schedule is not None


def test_worker_claims_and_executes_jobs(platform, principal, demo_dir: Path) -> None:  # type: ignore[no-untyped-def]
    with platform.unit_of_work(principal) as context:
        _register(context, demo_dir)
        context.pipelines.create(
            {
                "name": "worker_flow",
                "steps": [
                    {
                        "id": "ingest",
                        "uses": "read.source",
                        "with": {"source": "files", "object": "regions.csv", "dataset": "w"},
                    },
                ],
            }
        )
        context.pipelines.run("worker_flow")

    runner = JobRunner(
        platform, WorkerConfig(worker_id="test-worker", concurrency=1, poll_interval_s=0.01)
    )
    assert runner.drain(max_jobs=5) == 1

    with platform.unit_of_work(principal) as context:
        job = context.jobs.list(limit=1)[0]
        assert job.state == JobState.SUCCESS.value
        assert job.worker_id == "test-worker"


def test_only_one_worker_can_claim_a_job(platform, principal, demo_dir: Path) -> None:  # type: ignore[no-untyped-def]
    with platform.unit_of_work(principal) as context:
        _register(context, demo_dir)
        context.pipelines.create(
            {
                "name": "single",
                "steps": [
                    {
                        "id": "p",
                        "uses": "read.source",
                        "with": {"source": "files", "object": "regions.csv", "dataset": "s"},
                    }
                ],
            }
        )
        job = context.pipelines.run("single")

    with platform.db.session() as session:
        repo = platform.context(session, principal).jobs.repo
        first = repo.claim_next("worker-a", lease_seconds=60)
        second = repo.claim_next("worker-b", lease_seconds=60)

    assert first is not None and first.id == job.id
    assert second is None


def test_scheduler_fires_due_pipelines_and_reschedules(platform, principal, demo_dir: Path) -> None:  # type: ignore[no-untyped-def]
    with platform.unit_of_work(principal) as context:
        _register(context, demo_dir)
        context.pipelines.create(
            {
                "name": "scheduled",
                "schedule": {"cron": "*/5 * * * *"},
                "steps": [
                    {
                        "id": "p",
                        "uses": "read.source",
                        "with": {"source": "files", "object": "regions.csv", "dataset": "sch"},
                    }
                ],
            }
        )

    scheduler = Scheduler(platform)
    assert scheduler.tick() == 0  # nothing is due yet
    assert scheduler.tick(now=datetime.now(UTC) + timedelta(minutes=10)) == 1

    with platform.unit_of_work(principal) as context:
        jobs = context.jobs.list(limit=5)
        assert any(job.trigger == "schedule" for job in jobs)
        pipeline = context.pipelines.get("scheduled")
        assert pipeline.next_run_at is not None


def test_deleting_a_never_run_pipeline_does_not_crash(
    context: ServiceContext, demo_dir: Path
) -> None:
    """Regression: every pipeline has at least one PipelineVersion row from creation, and that
    FK (no cascade) made every delete raise a raw IntegrityError, always — not just when a
    pipeline had job history."""
    _register(context, demo_dir)
    context.pipelines.create(ANALYTICS_PIPELINE)

    context.pipelines.delete("sales_flow")

    from gdap.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        context.pipelines.get("sales_flow")


def test_deleting_a_pipeline_keeps_its_job_history(
    context: ServiceContext, demo_dir: Path
) -> None:
    """The CLI/API promise 'its run history is kept' — jobs carry their own pipeline_name/spec,
    so detaching them from the deleted pipeline (pipeline_id -> NULL) must not lose anything."""
    _register(context, demo_dir)
    context.pipelines.create(ANALYTICS_PIPELINE)
    job = context.pipelines.run("sales_flow")
    context.jobs.execute(job)
    job_id = job.id

    context.pipelines.delete("sales_flow")

    surviving = context.jobs.get(job_id)
    assert surviving.pipeline_id is None
    assert surviving.pipeline_name == "sales_flow"
    assert surviving.state == JobState.SUCCESS.value
    assert context.jobs.steps(job_id), "step history must survive the pipeline delete too"
