"""Trend and forecasting (§11).

Forecasting here is deliberately simple and transparent — OLS trend plus an optional seasonal
component — with an explicit prediction interval and an honest statement of its limits. A heavier
model belongs in :mod:`gdap.ml`, behind the model registry, not inlined in an insight.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl

from gdap.analytics.common import aggregate_series, evidence, format_number, percentage_change
from gdap.core.contracts import AnalysisResult, ChartSpec, Insight
from gdap.core.enums import AnalysisKind, InsightKind, Severity
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import require_columns

_PERIOD_DELTA = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "quarter": timedelta(days=91),
    "year": timedelta(days=365),
}


def analyse_trend(
    frame: pl.DataFrame,
    *,
    dataset: str,
    metric: str,
    time_column: str,
    granularity: str = "month",
    agg: str = "sum",
) -> AnalysisResult:
    require_columns(frame, [metric, time_column], context="trend analysis")
    series = aggregate_series(frame, time_column, metric, granularity=granularity, agg=agg)
    if series.height < 3:
        raise ValidationFailedError(
            f"need at least 3 {granularity} periods for a trend, found {series.height}"
        )

    values = series["value"].cast(pl.Float64).to_numpy()
    periods = [_label(v) for v in series["_bucket"].to_list()]
    x = np.arange(len(values), dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    fitted = slope * x + intercept
    residuals = values - fitted
    ss_total = float(((values - values.mean()) ** 2).sum())
    r_squared = float(1 - (residuals**2).sum() / ss_total) if ss_total else 0.0

    latest, first = float(values[-1]), float(values[0])
    total_change = percentage_change(latest, first)
    period_change = percentage_change(latest, float(values[-2])) if len(values) > 1 else None
    average_period_change = float(slope)
    direction = "increasing" if slope > 0 else ("decreasing" if slope < 0 else "flat")

    moving_average = (
        series["value"].rolling_mean(window_size=min(3, series.height), min_samples=1).to_numpy()
    )
    chart_data = [
        {
            "period": periods[i],
            "value": float(values[i]),
            "trend": float(fitted[i]),
            "moving_avg": float(moving_average[i]),
        }
        for i in range(len(values))
    ]

    insights = [
        Insight(
            kind=InsightKind.FACT,
            title=(
                f"{metric} is {direction} at {format_number(abs(average_period_change))} per {granularity}"
            ),
            detail=(
                f"Linear fit over {series.height} periods (R²={r_squared:.2f}); "
                f"{format_number(first)} → {format_number(latest)}"
                + (f" ({total_change:+.1f}%)" if total_change is not None else "")
                + "."
            ),
            severity=Severity.WARNING if (total_change or 0) < -10 else Severity.INFO,
            confidence=max(min(r_squared, 0.95), 0.3),
            evidence=[
                evidence(
                    f"dataset:{dataset}",
                    calculation=f"OLS on {agg}({metric}) per {granularity}",
                    values={
                        "slope": average_period_change,
                        "r_squared": r_squared,
                        "first": first,
                        "last": latest,
                    },
                    rows=frame.height,
                )
            ],
        )
    ]
    if period_change is not None and abs(period_change) > 15:
        insights.append(
            Insight(
                kind=InsightKind.FACT,
                title=f"Last {granularity} moved {period_change:+.1f}% versus the previous one",
                detail=(
                    f"{format_number(float(values[-2]))} → {format_number(latest)}. "
                    "A single-period swing may be seasonal — compare with the same period last year."
                ),
                severity=Severity.WARNING if period_change < 0 else Severity.INFO,
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation="last vs previous period",
                        values={"previous": float(values[-2]), "current": latest},
                    )
                ],
            )
        )
    if r_squared < 0.3:
        insights.append(
            Insight(
                kind=InsightKind.HYPOTHESIS,
                title="The series is noisy — a linear trend explains little of it",
                detail=(
                    f"R²={r_squared:.2f}. Consider a coarser granularity, a seasonal model, or "
                    "segmenting before drawing conclusions."
                ),
                confidence=0.4,
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.TREND,
        dataset=dataset,
        summary=(
            f"{metric} per {granularity}: {direction}, "
            f"{format_number(first)} → {format_number(latest)}"
            + (f" ({total_change:+.1f}%)" if total_change is not None else "")
        ),
        metrics={
            "slope_per_period": average_period_change,
            "r_squared": round(r_squared, 4),
            "first": first,
            "last": latest,
            "total_change_pct": total_change,
            "last_period_change_pct": period_change,
            "periods": series.height,
        },
        tables={"series": chart_data},
        charts=[
            ChartSpec(
                kind="line",
                title=f"{agg}({metric}) per {granularity}",
                x="period",
                y=["value", "trend", "moving_avg"],
                data=chart_data,
            )
        ],
        insights=insights,
        params={"metric": metric, "granularity": granularity, "agg": agg},
    )


def forecast(
    frame: pl.DataFrame,
    *,
    dataset: str,
    metric: str,
    time_column: str,
    granularity: str = "month",
    horizon: int = 3,
    agg: str = "sum",
    seasonality: int | None = None,
) -> AnalysisResult:
    """Trend + optional additive seasonality, with an 80% prediction interval."""
    require_columns(frame, [metric, time_column], context="forecast")
    series = aggregate_series(frame, time_column, metric, granularity=granularity, agg=agg)
    minimum = max(4, (seasonality or 0) * 2)
    if series.height < minimum:
        raise ValidationFailedError(
            f"forecasting needs at least {minimum} {granularity} periods, found {series.height}"
        )

    values = series["value"].cast(pl.Float64).to_numpy()
    x = np.arange(len(values), dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    trend = slope * x + intercept
    detrended = values - trend

    season_length = seasonality or _infer_seasonality(granularity, len(values))
    seasonal = np.zeros(season_length) if season_length else np.zeros(1)
    if season_length and len(values) >= season_length * 2:
        for phase in range(season_length):
            seasonal[phase] = float(detrended[phase::season_length].mean())
        seasonal -= seasonal.mean()

    fitted = trend + (
        np.array([seasonal[i % season_length] for i in range(len(values))]) if season_length else 0
    )
    residual_std = float(np.std(values - fitted, ddof=1)) if len(values) > 2 else 0.0
    interval = 1.28 * residual_std  # ~80% prediction interval

    last_period = series["_bucket"][-1]
    predictions: list[dict[str, Any]] = []
    for step in range(1, horizon + 1):
        index = len(values) - 1 + step
        point = float(slope * index + intercept)
        if season_length:
            point += float(seasonal[index % season_length])
        predictions.append(
            {
                "period": _label(_advance(last_period, granularity, step))
                if last_period is not None
                else f"t+{step}",
                "forecast": round(point, 4),
                "lower": round(point - interval, 4),
                "upper": round(point + interval, 4),
            }
        )

    history = [
        {
            "period": _label(series["_bucket"][i]),
            "value": float(values[i]),
            "fitted": float(fitted[i]),
        }
        for i in range(len(values))
    ]
    next_value = predictions[0]["forecast"]
    change = percentage_change(next_value, float(values[-1]))

    insights = [
        Insight(
            kind=InsightKind.INFERENCE,
            title=(
                f"Next {granularity} is projected at {format_number(next_value)}"
                + (f" ({change:+.1f}%)" if change is not None else "")
            ),
            detail=(
                f"Linear trend{' + seasonality (period ' + str(season_length) + ')' if season_length else ''}"
                f" fitted on {len(values)} periods; 80% interval "
                f"[{format_number(predictions[0]['lower'])}, {format_number(predictions[0]['upper'])}]."
            ),
            confidence=0.6 if residual_std and abs(next_value) > residual_std else 0.4,
            evidence=[
                evidence(
                    f"dataset:{dataset}",
                    calculation="OLS trend + additive seasonal means",
                    values={
                        "slope": float(slope),
                        "residual_std": residual_std,
                        "season_length": season_length,
                    },
                    rows=frame.height,
                )
            ],
        ),
        Insight(
            kind=InsightKind.HYPOTHESIS,
            title="Forecast assumes conditions do not change",
            detail=(
                "It extrapolates past behaviour only: promotions, outages, price changes or "
                "market shifts are not modelled. Treat the interval as the honest range."
            ),
            confidence=1.0,
        ),
    ]

    return AnalysisResult(
        kind=AnalysisKind.FORECAST,
        dataset=dataset,
        summary=(
            f"{horizon}-{granularity} forecast for {metric}: "
            + ", ".join(f"{p['period']}={format_number(p['forecast'])}" for p in predictions[:3])
        ),
        metrics={
            "horizon": horizon,
            "slope_per_period": float(slope),
            "residual_std": residual_std,
            "season_length": season_length,
            "next": next_value,
            "next_change_pct": change,
        },
        tables={"forecast": predictions, "history": history},
        charts=[
            ChartSpec(
                kind="line",
                title=f"{metric}: history and {horizon}-period forecast",
                x="period",
                y=["value", "forecast"],
                data=history
                + [
                    {
                        "period": p["period"],
                        "forecast": p["forecast"],
                        "lower": p["lower"],
                        "upper": p["upper"],
                    }
                    for p in predictions
                ],
                options={"interval": {"lower": "lower", "upper": "upper"}},
            )
        ],
        insights=insights,
        params={"metric": metric, "granularity": granularity, "horizon": horizon},
    )


def _advance(moment: Any, granularity: str, steps: int) -> Any:
    """Advance a period label by calendar steps (a month is a month, not 30 days)."""
    if granularity in {"month", "quarter", "year"} and hasattr(moment, "year"):
        months = {"month": 1, "quarter": 3, "year": 12}[granularity] * steps
        total = (moment.year * 12 + moment.month - 1) + months
        year, month = divmod(total, 12)
        return moment.replace(year=year, month=month + 1, day=1)
    return moment + _PERIOD_DELTA.get(granularity, timedelta(days=1)) * steps


def _infer_seasonality(granularity: str, length: int) -> int:
    candidates = {"day": 7, "week": 4, "month": 12, "quarter": 4, "year": 0}
    period = candidates.get(granularity, 0)
    return period if period and length >= period * 2 else 0


def _label(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)
