"""Anomaly detection (§11).

Four complementary methods, chosen by the shape of the question:

``zscore``    univariate, symmetric, fast — good for stable normal-ish metrics
``iqr``       univariate, robust to skew — the default for business data
``timeseries``rolling median + MAD over an aggregated series — catches level shifts
``isolation`` multivariate Isolation Forest — catches "weird combinations of normal values"

Every detection returns the score *and* the reason, because an anomaly a human cannot interpret
is just noise.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import polars as pl

from gdap.analytics.common import aggregate_series, evidence, format_number, records
from gdap.core.contracts import AnalysisResult, ChartSpec, Insight
from gdap.core.enums import AnalysisKind, InsightKind, Severity
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import is_numeric, require_columns

Method = Literal["auto", "zscore", "iqr", "timeseries", "isolation"]


def detect(
    frame: pl.DataFrame,
    *,
    dataset: str,
    columns: list[str] | None = None,
    method: Method = "auto",
    time_column: str | None = None,
    metric: str | None = None,
    granularity: str = "day",
    threshold: float = 3.0,
    max_anomalies: int = 200,
    contamination: float | str = 0.02,
) -> AnalysisResult:
    numeric = [
        name
        for name, dtype in frame.schema.items()
        if is_numeric(dtype) and (columns is None or name in columns)
    ]
    if method == "auto":
        method = (
            "timeseries"
            if (time_column and metric)
            else ("isolation" if len(numeric) >= 3 else "iqr")
        )

    if method == "timeseries":
        return _timeseries(
            frame,
            dataset=dataset,
            time_column=time_column,
            metric=metric,
            granularity=granularity,
            threshold=threshold,
        )
    if method == "isolation":
        return _isolation_forest(
            frame,
            dataset=dataset,
            columns=numeric,
            max_anomalies=max_anomalies,
            contamination=contamination,
        )
    return _univariate(
        frame,
        dataset=dataset,
        columns=numeric,
        method=method,
        threshold=threshold,
        max_anomalies=max_anomalies,
    )


def _univariate(
    frame: pl.DataFrame,
    *,
    dataset: str,
    columns: list[str],
    method: str,
    threshold: float,
    max_anomalies: int,
) -> AnalysisResult:
    if not columns:
        raise ValidationFailedError("no numeric column available for anomaly detection")

    findings: list[dict[str, Any]] = []
    per_column: dict[str, dict[str, float]] = {}
    for name in columns:
        series = frame[name].cast(pl.Float64, strict=False)
        clean = series.drop_nulls()
        if clean.len() < 5:
            continue
        array = clean.to_numpy()
        if method == "zscore":
            mean, std = float(array.mean()), float(array.std(ddof=1) or 0)
            if std == 0:
                continue
            scores = np.abs((array - mean) / std)
            limit = threshold
            bounds = (mean - threshold * std, mean + threshold * std)
        else:  # iqr
            q1, q3 = float(np.percentile(array, 25)), float(np.percentile(array, 75))
            iqr = q3 - q1
            if iqr == 0:
                continue
            bounds = (q1 - 1.5 * iqr, q3 + 1.5 * iqr)
            scores = np.maximum((bounds[0] - array) / iqr, (array - bounds[1]) / iqr)
            limit = 0.0

        flagged = np.where(scores > limit)[0]
        per_column[name] = {
            "anomalies": int(len(flagged)),
            "ratio": float(len(flagged) / len(array)),
            "lower_bound": bounds[0],
            "upper_bound": bounds[1],
        }
        indices = clean.to_frame().with_row_index("_row")["_row"].to_numpy()
        for position in flagged[: max_anomalies // max(len(columns), 1)]:
            value = float(array[position])
            findings.append(
                {
                    "column": name,
                    "row": int(indices[position]),
                    "value": value,
                    "score": round(float(scores[position]), 4),
                    "reason": (
                        f"{value:.4g} is outside the expected range "
                        f"[{bounds[0]:.4g}, {bounds[1]:.4g}]"
                    ),
                }
            )

    findings.sort(key=lambda row: row["score"], reverse=True)
    insights = [
        Insight(
            kind=InsightKind.FACT,
            title=f"{stats['anomalies']} anomalies in {name} ({stats['ratio']:.2%} of rows)",
            detail=(
                f"Values outside [{format_number(stats['lower_bound'])}, "
                f"{format_number(stats['upper_bound'])}] using the {method} method."
            ),
            severity=Severity.WARNING if stats["ratio"] > 0.02 else Severity.INFO,
            evidence=[
                evidence(
                    f"dataset:{dataset}",
                    calculation=f"{method}(threshold={threshold})",
                    values=dict(stats),
                    rows=frame.height,
                )
            ],
        )
        for name, stats in sorted(per_column.items(), key=lambda item: -item[1]["anomalies"])[:5]
        if stats["anomalies"]
    ]

    return AnalysisResult(
        kind=AnalysisKind.ANOMALY,
        dataset=dataset,
        summary=(
            f"{len(findings)} anomalous value(s) across {len(per_column)} column(s) "
            f"using the {method} method."
        ),
        metrics={
            "method": method,
            "anomalies": len(findings),
            "columns_checked": len(per_column),
        },
        tables={
            "anomalies": findings[:max_anomalies],
            "per_column": [{"column": name, **stats} for name, stats in per_column.items()],
        },
        charts=[
            ChartSpec(
                kind="box",
                title="Value distribution and outliers",
                y=list(per_column)[:6],
                data=records(frame.select(list(per_column)[:6]), limit=5000),
            )
        ]
        if per_column
        else [],
        insights=insights,
        params={"method": method, "threshold": threshold},
    )


def _timeseries(
    frame: pl.DataFrame,
    *,
    dataset: str,
    time_column: str | None,
    metric: str | None,
    granularity: str,
    threshold: float,
) -> AnalysisResult:
    if not time_column or not metric:
        raise ValidationFailedError("time series anomaly detection needs time_column and metric")
    require_columns(frame, [time_column, metric], context="anomaly detection")

    series = aggregate_series(frame, time_column, metric, granularity=granularity, agg="sum")
    if series.height < 5:
        raise ValidationFailedError(f"need at least 5 {granularity} buckets, found {series.height}")

    values = series["value"].cast(pl.Float64).to_numpy()
    window = max(3, min(7, len(values) // 3))
    rolling_median = series["value"].rolling_median(window_size=window, min_samples=1).to_numpy()
    deviation = values - rolling_median
    mad = float(np.median(np.abs(deviation - np.median(deviation)))) or 1e-9
    scores = 0.6745 * deviation / mad  # modified z-score

    anomalies: list[dict[str, Any]] = []
    for index, score in enumerate(scores):
        if abs(score) >= threshold:
            anomalies.append(
                {
                    "period": _label(series["_bucket"][index]),
                    "value": float(values[index]),
                    "expected": float(rolling_median[index]),
                    "score": round(float(score), 3),
                    "direction": "spike" if score > 0 else "drop",
                    "reason": (
                        f"{format_number(float(values[index]))} vs expected "
                        f"{format_number(float(rolling_median[index]))} "
                        f"(modified z={score:.1f})"
                    ),
                }
            )

    chart_data = [
        {
            "period": _label(series["_bucket"][i]),
            "value": float(values[i]),
            "expected": float(rolling_median[i]),
        }
        for i in range(len(values))
    ]

    insights = []
    for anomaly in sorted(anomalies, key=lambda row: abs(row["score"]), reverse=True)[:3]:
        insights.append(
            Insight(
                kind=InsightKind.FACT,
                title=f"{metric} {anomaly['direction']} in {anomaly['period']}",
                detail=anomaly["reason"],
                severity=Severity.CRITICAL
                if abs(anomaly["score"]) > threshold * 2
                else Severity.WARNING,
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation=f"rolling_median(window={window}) + modified z-score",
                        values={k: anomaly[k] for k in ("value", "expected", "score")},
                        rows=frame.height,
                    )
                ],
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.ANOMALY,
        dataset=dataset,
        summary=(
            f"{len(anomalies)} anomalous {granularity}(s) in {metric} out of {series.height} "
            f"periods analysed."
        ),
        metrics={
            "method": "timeseries",
            "anomalies": len(anomalies),
            "periods": series.height,
            "window": window,
        },
        tables={"anomalies": anomalies},
        charts=[
            ChartSpec(
                kind="line",
                title=f"{metric} per {granularity} vs expected",
                x="period",
                y=["value", "expected"],
                data=chart_data,
                options={"highlight": [a["period"] for a in anomalies]},
            )
        ],
        insights=insights,
        params={"metric": metric, "time_column": time_column, "granularity": granularity},
    )


def _isolation_forest(
    frame: pl.DataFrame,
    *,
    dataset: str,
    columns: list[str],
    max_anomalies: int,
    contamination: float | str = 0.02,
) -> AnalysisResult:
    from sklearn.ensemble import IsolationForest

    usable = [name for name in columns if frame[name].drop_nulls().len() > 10]
    if len(usable) < 2:
        raise ValidationFailedError("isolation forest needs at least two usable numeric columns")

    matrix = frame.select(usable).drop_nulls()
    if matrix.height < 20:
        raise ValidationFailedError("isolation forest needs at least 20 complete rows")

    array = matrix.to_numpy().astype(float)
    model = IsolationForest(n_estimators=200, contamination=contamination, random_state=42)
    predictions = model.fit_predict(array)
    scores = -model.score_samples(array)  # higher = more anomalous

    flagged = np.where(predictions == -1)[0]
    order = flagged[np.argsort(-scores[flagged])][:max_anomalies]
    anomalies: list[dict[str, Any]] = []
    means = array.mean(axis=0)
    stds = array.std(axis=0)
    for position in order:
        row = array[position]
        deviations = np.abs((row - means) / np.where(stds == 0, 1, stds))
        driver_index = int(np.argmax(deviations))
        anomalies.append(
            {
                "row": int(position),
                "score": round(float(scores[position]), 4),
                "main_driver": usable[driver_index],
                "reason": (
                    f"{usable[driver_index]}={row[driver_index]:.4g} is "
                    f"{deviations[driver_index]:.1f}σ from the mean, in an unusual combination"
                ),
                **{name: float(row[i]) for i, name in enumerate(usable)},
            }
        )

    ratio = len(flagged) / matrix.height
    insights = [
        Insight(
            kind=InsightKind.FACT,
            title=f"{len(flagged)} multivariate anomalies ({ratio:.2%} of complete rows)",
            detail=(
                "Isolation Forest flags rows whose *combination* of values is rare, even when "
                "each individual value looks normal."
            ),
            severity=Severity.WARNING if ratio > 0.05 else Severity.INFO,
            evidence=[
                evidence(
                    f"dataset:{dataset}",
                    calculation=f"IsolationForest(n_estimators=200, contamination={contamination})",
                    values={"flagged": int(len(flagged)), "rows": matrix.height},
                    rows=matrix.height,
                )
            ],
        )
    ]
    if anomalies:
        drivers: dict[str, int] = {}
        for anomaly in anomalies:
            drivers[anomaly["main_driver"]] = drivers.get(anomaly["main_driver"], 0) + 1
        top_driver = max(drivers.items(), key=lambda item: item[1])
        insights.append(
            Insight(
                kind=InsightKind.INFERENCE,
                title=f"'{top_driver[0]}' dominates the anomalous rows",
                detail=f"{top_driver[1]} of {len(anomalies)} flagged rows deviate most on '{top_driver[0]}'.",
                confidence=0.7,
                evidence=[
                    evidence(
                        f"dataset:{dataset}",
                        calculation="argmax(|z-score|) per flagged row",
                        values={"driver": top_driver[0], "count": top_driver[1]},
                    )
                ],
            )
        )

    return AnalysisResult(
        kind=AnalysisKind.ANOMALY,
        dataset=dataset,
        summary=f"{len(flagged)} multivariate anomalies across {len(usable)} features.",
        metrics={
            "method": "isolation_forest",
            "anomalies": int(len(flagged)),
            "rows": matrix.height,
            "features": len(usable),
        },
        tables={"anomalies": anomalies},
        charts=[
            ChartSpec(
                kind="scatter",
                title=f"{usable[0]} vs {usable[1]} (anomalies highlighted)",
                x=usable[0],
                y=usable[1],
                data=[
                    {
                        usable[0]: float(array[i][0]),
                        usable[1]: float(array[i][1]),
                        "anomaly": bool(predictions[i] == -1),
                    }
                    for i in range(min(matrix.height, 3000))
                ],
                options={"color_field": "anomaly"},
            )
        ],
        insights=insights,
        params={"features": usable},
    )


def _label(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)
