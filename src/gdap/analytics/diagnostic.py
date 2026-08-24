"""Diagnostic analytics (§11): *why* the numbers look like that.

Correlation, segmentation, period comparison and driver analysis. Every claim produced here
carries the calculation that generated it, because "region X is the problem" is only useful if the
reader can check it.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl

from gdap.analytics.common import evidence, format_number, percentage_change, records
from gdap.core.contracts import AnalysisResult, ChartSpec, Insight
from gdap.core.enums import AnalysisKind, InsightKind, Severity
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import categorical_columns, is_numeric, require_columns


def correlate(
    frame: pl.DataFrame, *, dataset: str, columns: list[str] | None = None, threshold: float = 0.5
) -> AnalysisResult:
    numeric = [
        name
        for name, dtype in frame.schema.items()
        if is_numeric(dtype) and (columns is None or name in columns) and frame[name].n_unique() > 1
    ]
    if len(numeric) < 2:
        raise ValidationFailedError(
            "correlation needs at least two numeric columns with variance",
            details={"numeric_columns": numeric},
        )
    matrix = frame.select(numeric).drop_nulls()
    if matrix.height < 3:
        raise ValidationFailedError("not enough complete rows to correlate")

    values = matrix.to_numpy().astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        correlation = np.corrcoef(values, rowvar=False)

    pairs: list[dict[str, Any]] = []
    for i, left in enumerate(numeric):
        for j, right in enumerate(numeric):
            if j <= i:
                continue
            value = correlation[i][j]
            if math.isnan(value):
                continue
            pairs.append({"left": left, "right": right, "correlation": round(float(value), 4)})
    pairs.sort(key=lambda row: abs(row["correlation"]), reverse=True)

    heatmap: list[dict[str, Any]] = [
        {"x": left, "y": right, "value": round(float(correlation[i][j]), 4)}
        for i, left in enumerate(numeric)
        for j, right in enumerate(numeric)
        if not math.isnan(correlation[i][j])
    ]

    insights = []
    for pair in pairs[:5]:
        value = pair["correlation"]
        if abs(value) < threshold:
            continue
        direction = "positively" if value > 0 else "negatively"
        insights.append(
            Insight(
                kind=InsightKind.INFERENCE,
                title=f"{pair['left']} and {pair['right']} move {direction} together (r={value:.2f})",
                detail=(
                    f"Pearson correlation of {value:.3f} over {matrix.height} complete rows. "
                    "Correlation is not causation — treat this as a lead, not a conclusion."
                ),
                confidence=min(abs(value), 0.95),
                severity=Severity.INFO,
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation=f"pearson({pair['left']}, {pair['right']})",
                        values={"r": value},
                        rows=matrix.height,
                    )
                ],
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.CORRELATION,
        dataset=dataset,
        summary=(
            f"Correlated {len(numeric)} numeric columns over {matrix.height} complete rows; "
            f"{sum(1 for p in pairs if abs(p['correlation']) >= threshold)} pair(s) above |{threshold}|."
        ),
        metrics={"columns": len(numeric), "rows": matrix.height, "pairs": len(pairs)},
        tables={"pairs": pairs[:100]},
        charts=[
            ChartSpec(
                kind="heatmap",
                title="Correlation matrix",
                x="x",
                y="y",
                data=heatmap,
                options={"value_field": "value", "scale": "diverging"},
            )
        ],
        insights=insights,
        params={"threshold": threshold},
    )


def segment(
    frame: pl.DataFrame,
    *,
    dataset: str,
    metric: str,
    dimension: str,
    agg: str = "sum",
    top: int = 20,
) -> AnalysisResult:
    """Break a metric down by a dimension and rank the segments."""
    require_columns(frame, [metric, dimension], context="segmentation")
    expression = {
        "sum": pl.col(metric).sum(),
        "mean": pl.col(metric).mean(),
        "median": pl.col(metric).median(),
        "count": pl.col(metric).count(),
    }.get(agg)
    if expression is None:
        raise ValidationFailedError(f"unsupported aggregation '{agg}'")

    grouped = (
        frame.group_by(dimension)
        .agg(
            expression.alias("value"),
            pl.len().alias("rows"),
            pl.col(metric).mean().alias("avg"),
        )
        .drop_nulls("value")
        .sort("value", descending=True)
    )
    total = float(grouped["value"].sum() or 0)
    grouped = grouped.with_columns(
        (pl.col("value") / total * 100).alias("share_pct")
        if total
        else pl.lit(0.0).alias("share_pct")
    )
    table = records(grouped.head(top))

    insights: list[Insight] = []
    if table:
        best, worst = table[0], table[-1]
        insights.append(
            Insight(
                kind=InsightKind.FACT,
                title=f"{best[dimension]} leads {metric} with {format_number(best['value'])}",
                detail=(
                    f"{best[dimension]} accounts for {best['share_pct']:.1f}% of total {metric} "
                    f"across {best['rows']} rows."
                ),
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        query=f"SELECT {dimension}, {agg}({metric}) GROUP BY {dimension}",
                        values={"segment": best[dimension], "value": best["value"]},
                        rows=int(best["rows"]),
                    )
                ],
            )
        )
        if len(table) > 1 and best["value"] and worst["value"] is not None:
            ratio = (best["value"] / worst["value"]) if worst["value"] else None
            if ratio and ratio > 3:
                insights.append(
                    Insight(
                        kind=InsightKind.INFERENCE,
                        title=f"{metric} is {ratio:.1f}× higher in {best[dimension]} than in {worst[dimension]}",
                        detail=(
                            "A gap this wide usually reflects a structural difference (mix, "
                            "coverage, pricing) rather than noise — worth a drill-down."
                        ),
                        confidence=0.7,
                        severity=Severity.WARNING,
                        evidence=[
                            evidence(
                                f"dataset:{dataset}",
                                calculation=f"{best[dimension]}/{worst[dimension]}",
                                values={"best": best["value"], "worst": worst["value"]},
                            )
                        ],
                    )
                )

    return AnalysisResult(
        kind=AnalysisKind.SEGMENTATION,
        dataset=dataset,
        summary=f"{metric} ({agg}) by {dimension}: {grouped.height} segment(s), total {format_number(total)}.",
        metrics={"segments": grouped.height, "total": total},
        tables={"segments": table},
        charts=[
            ChartSpec(
                kind="hbar",
                title=f"{agg}({metric}) by {dimension}",
                x="value",
                y=dimension,
                data=table[:15],
            )
        ],
        insights=insights,
        params={"metric": metric, "dimension": dimension, "agg": agg},
    )


def compare_periods(
    frame: pl.DataFrame,
    *,
    dataset: str,
    metric: str,
    time_column: str,
    dimension: str | None = None,
    granularity: str = "month",
) -> AnalysisResult:
    """Latest period vs the one before, overall and per dimension (contribution analysis)."""
    from gdap.analytics.common import bucket

    require_columns(frame, [metric, time_column], context="comparison")
    bucketed = bucket(frame, time_column, granularity).drop_nulls("_bucket")
    periods = bucketed.select("_bucket").unique().sort("_bucket")["_bucket"].to_list()
    if len(periods) < 2:
        raise ValidationFailedError(
            f"need at least two {granularity} periods to compare, found {len(periods)}"
        )
    current_period, previous_period = periods[-1], periods[-2]

    current = bucketed.filter(pl.col("_bucket") == current_period)
    previous = bucketed.filter(pl.col("_bucket") == previous_period)
    current_total = float(current[metric].sum() or 0)
    previous_total = float(previous[metric].sum() or 0)
    change = percentage_change(current_total, previous_total)

    table: list[dict[str, Any]] = []
    if dimension:
        require_columns(frame, [dimension], context="comparison")
        current_by = current.group_by(dimension).agg(pl.col(metric).sum().alias("current"))
        previous_by = previous.group_by(dimension).agg(pl.col(metric).sum().alias("previous"))
        joined = (
            current_by.join(previous_by, on=dimension, how="full", coalesce=True)
            .fill_null(0)
            .with_columns(
                (pl.col("current") - pl.col("previous")).alias("delta"),
            )
            .with_columns(
                pl.when(pl.col("previous") != 0)
                .then((pl.col("delta") / pl.col("previous").abs()) * 100)
                .otherwise(None)
                .alias("delta_pct"),
                (
                    pl.col("delta") / (current_total - previous_total) * 100
                    if current_total != previous_total
                    else pl.lit(0.0)
                ).alias("contribution_pct"),
            )
            .sort("delta")
        )
        table = records(joined)

    insights: list[Insight] = []
    direction = "grew" if (change or 0) >= 0 else "fell"
    insights.append(
        Insight(
            kind=InsightKind.FACT,
            title=(
                f"{metric} {direction} {abs(change):.1f}% versus the previous {granularity}"
                if change is not None
                else f"{metric} totalled {format_number(current_total)} this {granularity}"
            ),
            detail=(
                f"{format_number(previous_total)} → {format_number(current_total)} "
                f"({_period_label(previous_period)} → {_period_label(current_period)})."
            ),
            severity=Severity.WARNING if (change or 0) < -10 else Severity.INFO,
            evidence=[
                evidence(
                    f"dataset:{dataset}",
                    calculation=f"sum({metric}) per {granularity}",
                    values={
                        "current": current_total,
                        "previous": previous_total,
                        "change_pct": change,
                    },
                    rows=current.height + previous.height,
                )
            ],
        )
    )
    coverage = _period_coverage(current, previous, time_column)
    if coverage is not None and coverage < 0.9:
        insights.append(
            Insight(
                kind=InsightKind.HYPOTHESIS,
                title=f"The latest {granularity} is only {coverage:.0%} complete",
                detail=(
                    "It covers fewer days than the period it is compared against, so part of the "
                    "change is an artefact of the cut-off date rather than a real movement."
                ),
                confidence=0.9,
                severity=Severity.WARNING,
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation="distinct days in current period / previous period",
                        values={"coverage": coverage},
                    )
                ],
            )
        )

    if table and dimension:
        group = dimension  # non-optional inside this block; keeps the indexing honest
        movers = sorted(table, key=lambda row: row["delta"])
        worst, best = movers[0], movers[-1]
        if worst["delta"] < 0:
            insights.append(
                Insight(
                    kind=InsightKind.INFERENCE,
                    title=f"{worst[group]} drove the largest decline ({format_number(worst['delta'])})",
                    detail=(
                        f"{worst[group]} moved {format_number(worst['previous'])} → "
                        f"{format_number(worst['current'])}, "
                        f"{abs(worst.get('contribution_pct') or 0):.0f}% of the total change."
                    ),
                    severity=Severity.WARNING,
                    confidence=0.85,
                    evidence=[
                        evidence(
                            f"dataset:{dataset}",
                            calculation=f"delta by {dimension}",
                            values={k: worst[k] for k in ("current", "previous", "delta")},
                        )
                    ],
                )
            )
        if best["delta"] > 0:
            insights.append(
                Insight(
                    kind=InsightKind.FACT,
                    title=f"{best[group]} contributed the largest gain (+{format_number(best['delta'])})",
                    detail=f"{best[group]} grew {best.get('delta_pct') or 0:.1f}% over the period.",
                    evidence=[
                        evidence(
                            f"dataset:{dataset}",
                            calculation=f"delta by {dimension}",
                            values={k: best[k] for k in ("current", "previous", "delta")},
                        )
                    ],
                )
            )

    charts = [
        ChartSpec(
            kind="bar",
            title=f"{metric}: {_period_label(previous_period)} vs {_period_label(current_period)}",
            x="period",
            y="value",
            data=[
                {"period": _period_label(previous_period), "value": previous_total},
                {"period": _period_label(current_period), "value": current_total},
            ],
        )
    ]
    if table:
        charts.append(
            ChartSpec(
                kind="hbar",
                title=f"Change in {metric} by {dimension}",
                x="delta",
                y=dimension,
                data=table[:15],
                options={"diverging": True},
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.COMPARISON,
        dataset=dataset,
        summary=(
            f"{metric}: {format_number(previous_total)} → {format_number(current_total)}"
            + (f" ({change:+.1f}%)" if change is not None else "")
        ),
        metrics={
            "current": current_total,
            "previous": previous_total,
            "change_pct": change,
            "current_period": _period_label(current_period),
            "previous_period": _period_label(previous_period),
        },
        tables={"by_dimension": table} if table else {},
        charts=charts,
        insights=insights,
        params={"metric": metric, "dimension": dimension, "granularity": granularity},
    )


def drivers(
    frame: pl.DataFrame,
    *,
    dataset: str,
    metric: str,
    dimensions: list[str] | None = None,
    top: int = 5,
) -> AnalysisResult:
    """Which dimension best explains the variance of ``metric`` (η², effect size)."""
    require_columns(frame, [metric], context="driver analysis")
    candidates = dimensions or categorical_columns(frame, max_cardinality=50)
    candidates = [c for c in candidates if c != metric]
    if not candidates:
        raise ValidationFailedError("no categorical dimension available for driver analysis")

    values = frame[metric].cast(pl.Float64, strict=False)
    grand_mean = _as_float(values.mean())
    total_ss = _as_float(((values - grand_mean) ** 2).sum())

    rows: list[dict[str, Any]] = []
    for dimension in candidates:
        grouped = frame.group_by(dimension).agg(
            pl.col(metric).mean().alias("group_mean"),
            pl.len().alias("rows"),
        )
        between_ss = float(
            sum(
                row["rows"] * (row["group_mean"] - grand_mean) ** 2
                for row in grouped.iter_rows(named=True)
                if row["group_mean"] is not None
            )
        )
        eta_squared = between_ss / total_ss if total_ss else 0.0
        spread = (
            _as_float(grouped["group_mean"].max()) - _as_float(grouped["group_mean"].min())
            if grouped.height
            else 0.0
        )
        rows.append(
            {
                "dimension": dimension,
                "eta_squared": round(eta_squared, 4),
                "explained_pct": round(eta_squared * 100, 2),
                "groups": grouped.height,
                "spread": spread,
            }
        )
    rows.sort(key=lambda row: row["eta_squared"], reverse=True)

    insights = []
    for row in rows[:top]:
        if row["eta_squared"] < 0.05:
            continue
        insights.append(
            Insight(
                kind=InsightKind.INFERENCE,
                title=f"{row['dimension']} explains {row['explained_pct']:.1f}% of the variance in {metric}",
                detail=(
                    f"Across {row['groups']} groups, η² = {row['eta_squared']:.3f}. "
                    "This is an association: the driver may itself be caused by something else."
                ),
                confidence=min(0.5 + row["eta_squared"], 0.95),
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation="eta_squared = SS_between / SS_total",
                        values={"eta_squared": row["eta_squared"], "groups": row["groups"]},
                        rows=frame.height,
                    )
                ],
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.DRIVERS,
        dataset=dataset,
        summary=(
            f"Ranked {len(rows)} dimension(s) by how much of {metric}'s variance they explain; "
            f"top driver: {rows[0]['dimension']} ({rows[0]['explained_pct']:.1f}%)."
            if rows
            else "No driver could be computed."
        ),
        metrics={"top_driver": rows[0]["dimension"] if rows else None, "candidates": len(rows)},
        tables={"drivers": rows},
        charts=[
            ChartSpec(
                kind="hbar",
                title=f"Variance in {metric} explained by dimension",
                x="explained_pct",
                y="dimension",
                data=rows[:10],
            )
        ],
        insights=insights,
        params={"metric": metric, "dimensions": candidates},
    )


def _as_float(value: Any) -> float:
    """Narrow a Polars scalar (a deliberately broad union) to a float, or 0.0 when it is not one."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _period_coverage(
    current: pl.DataFrame, previous: pl.DataFrame, time_column: str
) -> float | None:
    """Fraction of the previous period's calendar days that the current period covers."""
    try:
        current_days = current.select(
            pl.col(time_column).cast(pl.Date, strict=False).n_unique()
        ).item()
        previous_days = previous.select(
            pl.col(time_column).cast(pl.Date, strict=False).n_unique()
        ).item()
    except Exception:  # pragma: no cover - non-date temporal types
        return None
    if not previous_days:
        return None
    return min(current_days / previous_days, 1.0)


def _period_label(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)
