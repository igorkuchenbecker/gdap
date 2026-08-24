"""Quality scoring and cleaning: measure honestly, change nothing silently."""

from __future__ import annotations

import polars as pl
import pytest

from gdap.cleaning import CleaningEngine
from gdap.core.contracts import Expectation, Principal
from gdap.core.enums import ApprovalMode, QualityDimension, Role, Severity
from gdap.core.errors import QualityGateError
from gdap.profiling import DataProfiler
from gdap.quality import QualityEngine, suggest_expectations
from gdap.security.rbac import permissions_for


@pytest.fixture
def profiled(sales_frame: pl.DataFrame):  # type: ignore[no-untyped-def]
    return sales_frame, DataProfiler().profile(sales_frame, dataset="sales")


def test_quality_score_is_bounded_and_weighted(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    report = QualityEngine().evaluate(frame, profile)
    assert 0 <= report.score <= 100
    assert len(report.dimensions) == len(QualityDimension)
    assert abs(sum(d.weight for d in report.dimensions) - 1.0) < 1e-6


def test_failed_expectation_is_reported_with_counts(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    report = QualityEngine().evaluate(
        frame, profile, expectations=[Expectation(column="revenue", kind="not_null")]
    )
    finding = next(f for f in report.findings if f.rule.startswith("not_null"))
    assert finding.failed_rows > 0
    assert finding.severity is Severity.CRITICAL
    assert report.status == "fail"  # a critical finding fails the report


def test_clean_data_scores_high() -> None:
    import datetime as dt

    frame = pl.DataFrame(
        {
            "id": range(200),
            "value": [float(i) for i in range(200)],
            "day": [dt.date.today() for _ in range(200)],
        }
    )
    profile = DataProfiler().profile(frame, dataset="clean")
    report = QualityEngine().evaluate(frame, profile)
    assert report.score > 90
    assert report.status == "pass"


def test_quality_gate_raises_below_threshold(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    engine = QualityEngine()
    report = engine.evaluate(frame, profile)
    with pytest.raises(QualityGateError):
        engine.gate(report, minimum=99.9)
    engine.gate(report, minimum=1.0)  # must not raise


def test_suggested_expectations_are_derived_from_the_profile(profiled) -> None:  # type: ignore[no-untyped-def]
    _frame, profile = profiled
    suggestions = suggest_expectations(profile)
    kinds = {expectation.kind for expectation in suggestions}
    assert "not_null" in kinds
    assert "row_count_between" in kinds


def test_cleaning_proposes_but_does_not_apply_destructive_fixes(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    engine = CleaningEngine()
    proposals = engine.propose(frame, profile, QualityEngine().evaluate(frame, profile))
    actions = {proposal.action for proposal in proposals}
    assert {"drop_duplicates", "trim_whitespace", "fill_missing"} & actions

    cleaned, result = engine.apply(frame, proposals)
    assert cleaned.height <= frame.height
    assert all(
        proposal.approval is not ApprovalMode.REQUIRES_APPROVAL for proposal in result.applied
    )
    assert any(proposal.approval is ApprovalMode.REQUIRES_APPROVAL for proposal in result.skipped)


def test_approved_proposal_is_applied(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    engine = CleaningEngine()
    proposals = engine.propose(frame, profile, QualityEngine().evaluate(frame, profile))
    gated = [p for p in proposals if p.approval is ApprovalMode.REQUIRES_APPROVAL]
    if not gated:
        pytest.skip("no gated proposal in this fixture")
    _cleaned, result = engine.apply(frame, proposals, approved_ids={gated[0].id})
    assert gated[0].id in {proposal.id for proposal in result.applied}


def test_policy_blocks_cleaning_without_permission(profiled) -> None:  # type: ignore[no-untyped-def]
    frame, profile = profiled
    engine = CleaningEngine()
    proposals = engine.propose(frame, profile, QualityEngine().evaluate(frame, profile))
    viewer = Principal(
        org_id="o", user_id="v", role=Role.VIEWER, permissions=permissions_for(Role.VIEWER)
    )
    cleaned, result = engine.apply(frame, proposals, principal=viewer)
    assert not result.applied
    assert cleaned.height == frame.height
    assert all("blocked by policy" in line for line in result.log)


def test_locale_aware_numeric_parsing() -> None:
    from gdap.cleaning.engine import _cast_numeric
    from gdap.core.contracts import CleaningProposal

    proposal = CleaningProposal(id="c", column="v", issue="text numbers", action="cast_numeric")
    european = pl.DataFrame({"v": ["1.203,45", "2.000,10"]})
    american = pl.DataFrame({"v": ["1,203.45", "2,000.10"]})
    assert _cast_numeric(european, proposal)[0]["v"].to_list() == [1203.45, 2000.10]
    assert _cast_numeric(american, proposal)[0]["v"].to_list() == [1203.45, 2000.10]
