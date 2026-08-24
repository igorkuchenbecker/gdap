"""Shared helpers for the analytics layer: evidence, formatting, temporal bucketing."""

from __future__ import annotations

from typing import Any

import polars as pl

from gdap.core.contracts import Evidence
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import is_numeric, is_temporal

GRANULARITIES = {
    "day": "1d",
    "week": "1w",
    "month": "1mo",
    "quarter": "1q",
    "year": "1y",
}


def evidence(
    source: str,
    *,
    query: str | None = None,
    calculation: str | None = None,
    values: dict[str, Any] | None = None,
    rows: int | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        query=query,
        calculation=calculation,
        values=values or {},
        rows_considered=rows,
    )


def pick_metric(frame: pl.DataFrame, metric: str | None) -> str:
    """Resolve the metric column, preferring an explicit one and failing loudly otherwise."""
    if metric:
        if metric not in frame.columns:
            raise ValidationFailedError(
                f"metric column '{metric}' not found", details={"available": frame.columns}
            )
        return metric
    numeric = [name for name, dtype in frame.schema.items() if is_numeric(dtype)]
    if not numeric:
        raise ValidationFailedError("no numeric column available to use as a metric")
    return numeric[0]


def pick_time_column(frame: pl.DataFrame, column: str | None) -> str | None:
    if column:
        if column not in frame.columns:
            raise ValidationFailedError(
                f"time column '{column}' not found", details={"available": frame.columns}
            )
        return column
    temporal = [name for name, dtype in frame.schema.items() if is_temporal(dtype)]
    return temporal[0] if temporal else None


def bucket(frame: pl.DataFrame, time_column: str, granularity: str) -> pl.DataFrame:
    """Truncate a temporal column to the requested granularity."""
    if granularity not in GRANULARITIES:
        raise ValidationFailedError(
            f"unsupported granularity '{granularity}'", details={"supported": sorted(GRANULARITIES)}
        )
    return frame.with_columns(
        pl.col(time_column)
        .cast(pl.Datetime, strict=False)
        .dt.truncate(GRANULARITIES[granularity])
        .alias("_bucket")
    )


def aggregate_series(
    frame: pl.DataFrame,
    time_column: str,
    metric: str,
    *,
    granularity: str = "month",
    agg: str = "sum",
) -> pl.DataFrame:
    """Return a two-column ``(_bucket, value)`` series ordered in time."""
    bucketed = bucket(frame, time_column, granularity)
    expression = {
        "sum": pl.col(metric).sum(),
        "mean": pl.col(metric).mean(),
        "median": pl.col(metric).median(),
        "count": pl.col(metric).count(),
        "min": pl.col(metric).min(),
        "max": pl.col(metric).max(),
    }.get(agg)
    if expression is None:
        raise ValidationFailedError(
            f"unsupported aggregation '{agg}'",
            details={"supported": ["sum", "mean", "median", "count", "min", "max"]},
        )
    return (
        bucketed.group_by("_bucket")
        .agg(expression.alias("value"))
        .drop_nulls("_bucket")
        .sort("_bucket")
    )


def percentage_change(current: float, previous: float) -> float | None:
    if previous in (0, None):
        return None
    return (current - previous) / abs(previous) * 100


def format_number(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.{decimals}f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.{decimals}f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.{decimals}f}K"
    return f"{value:.{decimals}f}"


def records(frame: pl.DataFrame, *, limit: int = 200) -> list[dict[str, Any]]:
    from gdap.core.frames import to_records

    return to_records(frame, limit=limit)
