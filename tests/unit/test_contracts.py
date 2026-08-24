"""Contracts are the platform's type system — these guard their invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gdap.core.contracts import (
    ColumnSchema,
    DatasetSchema,
    Evidence,
    Insight,
    PipelineSpec,
    Principal,
    ScheduleSpec,
    SourceSpec,
    StepSpec,
)
from gdap.core.enums import InsightKind, JobState, Permission, Role, SourceType


def test_schema_diff_detects_breaking_changes() -> None:
    before = DatasetSchema(
        columns=[ColumnSchema(name="a", dtype="Int64"), ColumnSchema(name="b", dtype="String")]
    )
    after = DatasetSchema(
        columns=[ColumnSchema(name="a", dtype="Float64"), ColumnSchema(name="c", dtype="String")]
    )
    diff = before.diff(after)
    assert diff.added == ["c"]
    assert diff.removed == ["b"]
    assert diff.type_changed == ["a"]
    assert diff.is_breaking


def test_schema_diff_additive_is_not_breaking() -> None:
    before = DatasetSchema(columns=[ColumnSchema(name="a", dtype="Int64")])
    after = DatasetSchema(
        columns=[ColumnSchema(name="a", dtype="Int64"), ColumnSchema(name="b", dtype="Int64")]
    )
    diff = before.diff(after)
    assert diff.added == ["b"]
    assert not diff.is_breaking


def test_schema_fingerprint_is_order_independent() -> None:
    left = DatasetSchema(
        columns=[ColumnSchema(name="a", dtype="Int64"), ColumnSchema(name="b", dtype="String")]
    )
    right = DatasetSchema(
        columns=[ColumnSchema(name="b", dtype="String"), ColumnSchema(name="a", dtype="Int64")]
    )
    assert left.fingerprint() == right.fingerprint()


def test_source_spec_refuses_inline_secrets() -> None:
    with pytest.raises(ValidationError, match="inline secret"):
        SourceSpec(
            name="db",
            type=SourceType.SQL,
            connector="sql",
            config={"password": "hunter2"},
        )


def test_source_spec_accepts_secret_references() -> None:
    spec = SourceSpec(
        name="db",
        type=SourceType.SQL,
        connector="sql",
        config={"host": "db.internal"},
        secret_refs={"password": "env:PGPASS"},
    )
    assert spec.secret_refs["password"] == "env:PGPASS"


def test_fact_insight_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        Insight(kind=InsightKind.FACT, title="revenue fell", detail="…")

    ok = Insight(
        kind=InsightKind.FACT,
        title="revenue fell",
        detail="…",
        evidence=[Evidence(source="dataset:sales", calculation="sum(revenue)")],
    )
    assert ok.evidence[0].source == "dataset:sales"


def test_hypothesis_does_not_require_evidence() -> None:
    insight = Insight(kind=InsightKind.HYPOTHESIS, title="maybe seasonal", detail="…")
    assert insight.confidence == 1.0


def test_schedule_requires_exactly_one_form() -> None:
    with pytest.raises(ValidationError):
        ScheduleSpec()
    with pytest.raises(ValidationError):
        ScheduleSpec(cron="* * * * *", every="5m")
    assert ScheduleSpec(cron="0 6 * * *").cron == "0 6 * * *"


def test_pipeline_fingerprint_changes_with_steps() -> None:
    first = PipelineSpec(
        name="p", steps=[StepSpec(uses="read.dataset", **{"with": {"dataset": "a"}})]
    )
    second = PipelineSpec(
        name="p", steps=[StepSpec(uses="read.dataset", **{"with": {"dataset": "b"}})]
    )
    assert first.fingerprint() != second.fingerprint()


def test_principal_permissions_and_admin_wildcard() -> None:
    viewer = Principal(org_id="o", user_id="u", role=Role.VIEWER, permissions=frozenset())
    assert not viewer.has(Permission.DATASET_READ)

    admin = Principal(
        org_id="o", user_id="a", role=Role.ADMIN, permissions=frozenset({Permission.ADMIN})
    )
    assert admin.has(Permission.SQL_DESTRUCTIVE)

    system = Principal.system("o", "worker")
    assert system.is_system and system.has(Permission.PIPELINE_RUN)


def test_job_state_terminality() -> None:
    assert JobState.SUCCESS.is_terminal
    assert JobState.FAILED.is_terminal
    assert not JobState.RUNNING.is_terminal
    assert not JobState.AWAITING_APPROVAL.is_terminal
