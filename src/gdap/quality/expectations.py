"""Expectation evaluation.

An :class:`Expectation` is a declarative, testable statement about data. Each kind is implemented
once here and returns either ``None`` (passed) or a :class:`QualityFinding` describing exactly how
many rows failed and what they looked like — a finding is evidence, not an opinion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl

from gdap.core.contracts import Expectation, QualityFinding
from gdap.core.enums import QualityDimension, Severity
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import json_safe

SAMPLE_SIZE = 5


def evaluate(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    """Evaluate one expectation. Returns ``None`` when the data satisfies it."""
    handler = _HANDLERS.get(expectation.kind)
    if handler is None:  # pragma: no cover - guarded by the contract's Literal
        raise ValidationFailedError(f"unknown expectation kind '{expectation.kind}'")

    if expectation.column is not None and expectation.column not in frame.columns:
        return QualityFinding(
            dimension=QualityDimension.INTEGRITY,
            severity=expectation.severity,
            column=expectation.column,
            rule=expectation.label(),
            message=f"column '{expectation.column}' does not exist",
            failed_rows=frame.height,
            failed_ratio=1.0,
            suggestion="fix the expectation or the upstream schema",
        )
    return handler(frame, expectation)


def _finding(
    expectation: Expectation,
    frame: pl.DataFrame,
    failed: pl.DataFrame | int,
    message: str,
    *,
    suggestion: str | None = None,
    sample_column: str | None = None,
) -> QualityFinding | None:
    if isinstance(failed, int):
        count, sample = failed, []
    else:
        count = failed.height
        column = sample_column or expectation.column
        sample = (
            [json_safe(v) for v in failed[column].head(SAMPLE_SIZE).to_list()]
            if column and column in failed.columns
            else []
        )
    if count == 0:
        return None
    return QualityFinding(
        dimension=expectation.dimension,
        severity=expectation.severity,
        column=expectation.column,
        rule=expectation.label(),
        message=message.format(count=count, total=frame.height),
        failed_rows=count,
        failed_ratio=count / frame.height if frame.height else 0.0,
        sample=sample,
        suggestion=suggestion,
    )


def _not_null(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    failed = int(frame[column].null_count())
    return _finding(
        expectation,
        frame,
        failed,
        "{count} of {total} rows have a null value",
        suggestion="fill, drop or fix upstream",
    )


def _unique(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    series = frame[column].drop_nulls()
    duplicates = series.len() - series.n_unique()
    return _finding(
        expectation,
        frame,
        duplicates,
        "{count} duplicate values",
        suggestion="deduplicate or use a composite key",
    )


def _in_range(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    minimum = expectation.params.get("min")
    maximum = expectation.params.get("max")
    condition = pl.col(column).is_not_null()
    if minimum is not None:
        condition = condition & (pl.col(column) < minimum)
    if maximum is not None:
        below = pl.col(column).is_not_null() & (pl.col(column) > maximum)
        condition = (condition | below) if minimum is not None else below
    failed = frame.filter(condition)
    return _finding(
        expectation,
        frame,
        failed,
        f"{{count}} values outside [{minimum}, {maximum}]",
        suggestion="clip, drop or investigate the source system",
    )


def _in_set(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    allowed = expectation.params.get("values", [])
    failed = frame.filter(pl.col(column).is_not_null() & ~pl.col(column).is_in(allowed))
    return _finding(
        expectation,
        frame,
        failed,
        f"{{count}} values outside the allowed set ({len(allowed)} members)",
        suggestion="normalise categories or extend the allowed set",
    )


def _matches_regex(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    pattern = str(expectation.params.get("pattern", ".*"))
    failed = frame.filter(
        pl.col(column).is_not_null()
        & ~pl.col(column).cast(pl.Utf8, strict=False).str.contains(pattern)
    )
    return _finding(
        expectation,
        frame,
        failed,
        f"{{count}} values do not match /{pattern}/",
        suggestion="normalise the format at ingestion time",
    )


def _of_type(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    expected = str(expectation.params.get("dtype", ""))
    actual = str(frame.schema[column])
    if actual.lower().startswith(expected.lower()):
        return None
    return QualityFinding(
        dimension=expectation.dimension,
        severity=expectation.severity,
        column=column,
        rule=expectation.label(),
        message=f"expected type {expected}, found {actual}",
        failed_rows=frame.height,
        failed_ratio=1.0,
        suggestion=f"cast '{column}' to {expected} in the pipeline",
    )


def _row_count_between(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    minimum = int(expectation.params.get("min", 0))
    maximum = expectation.params.get("max")
    if frame.height >= minimum and (maximum is None or frame.height <= int(maximum)):
        return None
    return QualityFinding(
        dimension=expectation.dimension,
        severity=expectation.severity,
        rule=expectation.label(),
        message=f"row count {frame.height} outside [{minimum}, {maximum}]",
        failed_rows=abs(frame.height - minimum),
        failed_ratio=1.0,
        suggestion="check for a partial load or an upstream outage",
    )


def _freshness(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    column = expectation.column or ""
    max_age_hours = float(expectation.params.get("max_age_hours", 24))
    series = frame[column].drop_nulls()
    if series.is_empty():
        return _finding(expectation, frame, frame.height, "no timestamps to evaluate freshness")
    try:
        maximum = series.cast(pl.Datetime, strict=False).max()
    except Exception:
        return None
    # Polars scalars are a broad union; anything that is not a datetime cannot be aged.
    if not isinstance(maximum, datetime):
        return None
    newest = maximum if maximum.tzinfo else maximum.replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - newest).total_seconds() / 3600
    if age_hours <= max_age_hours:
        return None
    return QualityFinding(
        dimension=QualityDimension.TIMELINESS,
        severity=expectation.severity,
        column=column,
        rule=expectation.label(),
        message=f"most recent value is {age_hours:.1f}h old (limit {max_age_hours:.0f}h)",
        failed_rows=0,
        failed_ratio=0.0,
        sample=[newest.isoformat()],
        suggestion="check the ingestion schedule or the upstream feed",
    )


def _custom_sql(frame: pl.DataFrame, expectation: Expectation) -> QualityFinding | None:
    """Business rule expressed as SQL that must return zero rows."""
    from gdap.storage.query import DuckDBEngine

    sql = str(expectation.params.get("sql", ""))
    with DuckDBEngine() as engine:
        engine.register("data", frame)
        failed = engine.query(sql, limit=1000)
    return _finding(
        expectation,
        frame,
        failed.height,
        "{count} rows violate the business rule",
        suggestion=expectation.description or "review the rule and the source data",
    )


_HANDLERS: dict[str, Any] = {
    "not_null": _not_null,
    "unique": _unique,
    "in_range": _in_range,
    "in_set": _in_set,
    "matches_regex": _matches_regex,
    "of_type": _of_type,
    "row_count_between": _row_count_between,
    "freshness": _freshness,
    "custom_sql": _custom_sql,
}


def default_severity(dimension: QualityDimension) -> Severity:
    return (
        Severity.CRITICAL
        if dimension
        in {
            QualityDimension.COMPLETENESS,
            QualityDimension.VALIDITY,
            QualityDimension.INTEGRITY,
        }
        else Severity.WARNING
    )
