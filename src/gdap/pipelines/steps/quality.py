"""Quality steps: profiling, validation, gates and cleaning."""

from __future__ import annotations

from typing import Any

from gdap.cleaning.engine import CleaningEngine
from gdap.core.contracts import CleaningProposal, Expectation, Insight, StepSpec
from gdap.core.enums import ApprovalMode, DataClassification, InsightKind, Severity
from gdap.core.errors import QualityGateError, ValidationFailedError
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step
from gdap.profiling.profiler import DataProfiler
from gdap.quality.engine import QualityEngine, suggest_expectations


@register_step(
    "profile",
    description="Profile the working frame (schema, statistics, semantics, recommendations).",
    category="quality",
    options={"dataset": "name used in the profile (defaults to the current frame)"},
)
def profile_step(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    name = str(context.option(step, "dataset", context.current or "data"))
    profile = DataProfiler().profile(frame, dataset=name)
    context.metrics["duplicate_rows"] = profile.duplicate_rows
    insights = [
        Insight(
            kind=InsightKind.RECOMMENDATION,
            title=recommendation,
            detail="Detected while profiling the pipeline's working data.",
            confidence=0.8,
        )
        for recommendation in profile.recommendations[:5]
    ]
    context.insights.extend(insights)
    return StepOutcome(
        frame=frame,
        message=(
            f"profiled {profile.rows:,} rows × {profile.columns} columns; "
            f"{len(profile.recommendations)} recommendation(s)"
        ),
        metrics={
            "rows": profile.rows,
            "columns": profile.columns,
            "duplicate_rows": profile.duplicate_rows,
            "candidate_keys": profile.candidate_keys,
        },
        insights=insights,
    )


@register_step(
    "validate.schema",
    description="Assert the working frame has the expected columns and types.",
    category="quality",
    options={
        "columns": "required column names",
        "types": "mapping column -> expected polars type prefix",
        "mode": "strict (no extra columns) | lenient",
    },
)
def validate_schema(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    required = list(context.option(step, "columns", []) or [])
    types: dict[str, str] = context.option(step, "types", {}) or {}
    mode = str(context.option(step, "mode", "lenient"))

    missing = [name for name in required if name not in frame.columns]
    wrong = {
        name: str(frame.schema[name])
        for name, expected in types.items()
        if name in frame.columns
        and not str(frame.schema[name]).lower().startswith(str(expected).lower())
    }
    extra = (
        [name for name in frame.columns if required and name not in required]
        if mode == "strict"
        else []
    )
    if missing or wrong or extra:
        raise ValidationFailedError(
            "schema validation failed",
            details={"missing": missing, "wrong_types": wrong, "unexpected": extra, "mode": mode},
        )
    return StepOutcome(
        frame=frame,
        message=f"schema valid ({len(required)} required column(s), {len(types)} type check(s))",
        metrics={"schema_checks": len(required) + len(types)},
    )


@register_step(
    "validate.expectations",
    description="Evaluate data expectations and score quality across seven dimensions.",
    category="quality",
    options={
        "expectations": "list of expectation objects",
        "auto": "derive expectations from the profile (default false)",
        "min_score": "fail the step below this quality score",
        "dataset": "dataset name recorded in the report",
    },
)
def validate_expectations(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    name = str(context.option(step, "dataset", context.current or "data"))
    profile = DataProfiler().profile(frame, dataset=name)

    declared = [
        Expectation.model_validate(item) for item in context.option(step, "expectations", []) or []
    ]
    if context.option(step, "auto", False) and not declared:
        declared = suggest_expectations(profile)

    report = QualityEngine(context.services.settings.quality).evaluate(
        frame, profile, expectations=declared
    )
    context.metrics["quality_score"] = report.score
    context.metrics["quality_status"] = report.status

    insights = [
        Insight(
            kind=InsightKind.FACT,
            title=f"Data quality {report.status}: {report.score:.1f}/100",
            detail="; ".join(finding.message for finding in report.findings[:3]) or "no findings",
            severity=Severity.CRITICAL
            if report.status == "fail"
            else (Severity.WARNING if report.status == "warn" else Severity.INFO),
            evidence=[
                {  # type: ignore[list-item]
                    "source": f"dataset:{name}",
                    "calculation": "weighted mean of seven quality dimensions",
                    "values": {
                        dimension.dimension.value: dimension.score
                        for dimension in report.dimensions
                    },
                    "rows_considered": report.rows_checked,
                }
            ],
        )
    ]
    context.insights.extend(insights)

    minimum = context.option(step, "min_score")
    if minimum is not None and report.score < float(minimum):
        raise QualityGateError(
            f"quality score {report.score:.1f} is below the required {float(minimum):.1f}",
            details={
                "score": report.score,
                "required": float(minimum),
                "findings": [finding.message for finding in report.findings[:10]],
            },
        )
    return StepOutcome(
        frame=frame,
        message=f"quality {report.status} ({report.score:.1f}/100), {len(report.findings)} finding(s)",
        metrics={
            "quality_score": report.score,
            "quality_status": report.status,
            "findings": len(report.findings),
            "expectations": report.expectations_evaluated,
        },
        insights=insights,
    )


@register_step(
    "quality.gate",
    description="Stop the pipeline when the recorded quality score is too low.",
    category="quality",
    options={"min_score": "minimum acceptable score (required)"},
)
def quality_gate(context: StepContext, step: StepSpec) -> StepOutcome:
    minimum = float(context.option(step, "min_score", required=True))
    score = context.metrics.get("quality_score")
    if score is None:
        raise ValidationFailedError(
            "no quality score recorded — run validate.expectations before quality.gate"
        )
    if float(score) < minimum:
        raise QualityGateError(
            f"quality gate failed: {float(score):.1f} < {minimum:.1f}",
            details={"score": score, "threshold": minimum},
        )
    return StepOutcome(message=f"quality gate passed ({float(score):.1f} ≥ {minimum:.1f})")


@register_step(
    "clean.auto",
    description="Propose cleaning fixes from the profile and apply the approved ones.",
    read_only=False,
    approval=ApprovalMode.AUTO_WITH_VALIDATION,
    category="cleaning",
    options={
        "apply": "auto | validated | all (default auto)",
        "actions": "restrict to these action names",
        "dataset": "dataset name used for classification and reporting",
    },
)
def clean_auto(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    name = str(context.option(step, "dataset", context.current or "data"))
    profile = DataProfiler().profile(frame, dataset=name)
    quality = QualityEngine(context.services.settings.quality).evaluate(frame, profile)

    engine = CleaningEngine(policy=context.services.policy)
    proposals = engine.propose(frame, profile, quality)

    allowed_actions = context.option(step, "actions")
    if allowed_actions:
        proposals = [p for p in proposals if p.action in set(allowed_actions)]

    mode = str(context.option(step, "apply", "auto"))
    allow_modes = {ApprovalMode.AUTO}
    if mode in {"validated", "all"}:
        allow_modes.add(ApprovalMode.AUTO_WITH_VALIDATION)
    approved = set(context.approved_steps) if mode == "all" else set()
    if mode == "all":
        approved |= {proposal.id for proposal in proposals}

    cleaned, result = engine.apply(
        frame,
        proposals,
        principal=context.principal,
        classification=DataClassification.INTERNAL,
        allow_modes=allow_modes,
        approved_ids=approved,
    )
    context.publish(step.output or context.current or "data", cleaned)

    pending = [p for p in result.skipped if p.approval is ApprovalMode.REQUIRES_APPROVAL]
    insights = [
        Insight(
            kind=InsightKind.RECOMMENDATION,
            title=f"{proposal.action} on '{proposal.column or 'dataset'}' awaits approval",
            detail=f"{proposal.issue} — {proposal.rationale}",
            severity=Severity.WARNING,
            confidence=proposal.confidence,
        )
        for proposal in pending[:5]
    ]
    context.insights.extend(insights)

    return StepOutcome(
        frame=cleaned,
        message=(
            f"applied {len(result.applied)} fix(es), {len(result.skipped)} skipped; "
            f"rows {result.rows_before:,} → {result.rows_after:,}"
        ),
        metrics={
            "fixes_applied": len(result.applied),
            "fixes_skipped": len(result.skipped),
            "cells_changed": result.cells_changed,
            "rows_before": result.rows_before,
            "rows_after": result.rows_after,
            "applied_actions": [proposal.action for proposal in result.applied],
        },
        insights=insights,
    )


@register_step(
    "clean.missing",
    description="Fill missing values with an explicit strategy.",
    read_only=False,
    category="cleaning",
    options={
        "strategy": "mean|median|mode|zero|constant|forward_fill (default median)",
        "columns": "columns to fill (default: all with nulls)",
        "value": "value for the constant strategy",
    },
)
def clean_missing(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    strategy = str(context.option(step, "strategy", "median"))
    columns = context.option(step, "columns") or [
        name for name in frame.columns if frame[name].null_count()
    ]
    engine = CleaningEngine()
    filled = 0
    working = frame
    for column in columns:
        if column not in working.columns:
            continue
        proposal = CleaningProposal(
            id=f"fill-{column}",
            column=column,
            issue="missing values",
            action="fill_missing",
            params={"strategy": strategy, "value": context.option(step, "value")},
        )
        working, result = engine.apply(working, [proposal], allow_modes={ApprovalMode.AUTO})
        filled += result.cells_changed
    context.publish(step.output or context.current or "data", working)
    return StepOutcome(
        frame=working,
        message=f"filled {filled:,} missing value(s) using '{strategy}'",
        metrics={"values_filled": filled, "strategy": strategy, "columns": list(columns)},
    )


@register_step(
    "clean.outliers",
    description="Clip or remove statistical outliers (explicit, never silent).",
    read_only=False,
    approval=ApprovalMode.AUTO_WITH_VALIDATION,
    category="cleaning",
    options={
        "columns": "numeric columns to treat",
        "action": "clip | remove | flag (default clip)",
        "factor": "IQR multiplier (default 1.5)",
    },
)
def clean_outliers(context: StepContext, step: StepSpec) -> StepOutcome:
    import polars as pl

    frame = context.frame(step.input)
    action = str(context.option(step, "action", "clip"))
    factor = float(context.option(step, "factor", 1.5))
    columns = context.option(step, "columns") or [
        name for name, dtype in frame.schema.items() if dtype.is_numeric()
    ]

    working = frame
    affected = 0
    for column in columns:
        series = working[column].cast(pl.Float64, strict=False).drop_nulls()
        if series.len() < 5:
            continue
        q1 = float(series.quantile(0.25) or 0)
        q3 = float(series.quantile(0.75) or 0)
        iqr = q3 - q1
        if iqr <= 0:
            continue
        lower, upper = q1 - factor * iqr, q3 + factor * iqr
        mask = (pl.col(column) < lower) | (pl.col(column) > upper)
        count = int(working.select(mask.sum()).item() or 0)
        if not count:
            continue
        affected += count
        if action == "clip":
            working = working.with_columns(pl.col(column).clip(lower, upper))
        elif action == "remove":
            working = working.filter(~mask.fill_null(False))
        elif action == "flag":
            working = working.with_columns(mask.fill_null(False).alias(f"{column}_is_outlier"))
        else:
            raise ValidationFailedError(
                f"unknown outlier action '{action}'",
                details={"supported": ["clip", "remove", "flag"]},
            )
    context.publish(step.output or context.current or "data", working)
    return StepOutcome(
        frame=working,
        message=f"{action}ped {affected:,} outlier value(s) across {len(columns)} column(s)",
        metrics={"outliers": affected, "action": action, "factor": factor},
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
