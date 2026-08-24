"""Descriptive analytics (§11): what the data *is*."""

from __future__ import annotations

from typing import Any

import polars as pl

from gdap.analytics.common import evidence, format_number, records
from gdap.core.contracts import AnalysisResult, ChartSpec, Insight
from gdap.core.enums import AnalysisKind, InsightKind, Severity
from gdap.core.frames import categorical_columns, is_numeric, to_float


def describe(
    frame: pl.DataFrame,
    *,
    dataset: str,
    columns: list[str] | None = None,
    max_charts: int = 4,
) -> AnalysisResult:
    selected = columns or frame.columns
    numeric = [c for c in selected if c in frame.columns and is_numeric(frame.schema[c])]
    categorical = [c for c in categorical_columns(frame) if c in selected]

    summary_rows: list[dict[str, Any]] = []
    for name in numeric:
        series = frame[name].drop_nulls()
        if series.is_empty():
            continue
        summary_rows.append(
            {
                "column": name,
                "count": int(series.len()),
                "missing": int(frame[name].null_count()),
                "mean": to_float(series.mean()),
                "median": to_float(series.median()),
                "std": to_float(series.std()) if series.len() > 1 else 0.0,
                "min": to_float(series.min()),
                "p25": to_float(series.quantile(0.25)),
                "p75": to_float(series.quantile(0.75)),
                "max": to_float(series.max()),
                "sum": to_float(series.sum()),
            }
        )

    category_tables: dict[str, list[dict[str, Any]]] = {}
    for name in categorical[:5]:
        counts = (
            frame.group_by(name).agg(pl.len().alias("rows")).sort("rows", descending=True).head(15)
        )
        category_tables[f"top_{name}"] = records(counts)

    charts: list[ChartSpec] = []
    for name in numeric[:max_charts]:
        series = frame[name].drop_nulls()
        if series.len() < 5:
            continue
        bin_counts, edges = _histogram(series)
        charts.append(
            ChartSpec(
                kind="histogram",
                title=f"Distribution of {name}",
                x=name,
                y="rows",
                data=[
                    {
                        "bin": f"{edges[i]:.4g}",
                        "start": edges[i],
                        "end": edges[i + 1],
                        "rows": counts[i],
                    }
                    for i in range(len(counts))
                ],
            )
        )
    for name in categorical[: max(0, max_charts - len(charts))]:
        table = category_tables.get(f"top_{name}", [])[:10]
        if table:
            charts.append(
                ChartSpec(
                    kind="hbar",
                    title=f"Rows by {name}",
                    x="rows",
                    y=name,
                    data=table,
                )
            )

    insights: list[Insight] = []
    for row in sorted(summary_rows, key=lambda r: r["missing"], reverse=True)[:3]:
        if row["missing"]:
            ratio = row["missing"] / max(row["count"] + row["missing"], 1)
            insights.append(
                Insight(
                    kind=InsightKind.FACT,
                    title=f"{row['column']} has {ratio:.1%} missing values",
                    detail=(
                        f"{row['missing']} of {row['count'] + row['missing']} rows are null in "
                        f"'{row['column']}'."
                    ),
                    severity=Severity.WARNING if ratio > 0.05 else Severity.INFO,
                    evidence=[
                        evidence(
                            f"dataset:{dataset}",
                            calculation=f"null_count('{row['column']}')",
                            values={"missing": row["missing"], "count": row["count"]},
                        )
                    ],
                )
            )
    for row in summary_rows[:3]:
        if row["std"] and row["mean"]:
            cv = abs(row["std"] / row["mean"])
            if cv > 1:
                insights.append(
                    Insight(
                        kind=InsightKind.INFERENCE,
                        title=f"{row['column']} is highly dispersed (CV={cv:.2f})",
                        detail=(
                            f"Standard deviation ({format_number(row['std'])}) exceeds the mean "
                            f"({format_number(row['mean'])}); averages will be misleading — prefer "
                            "the median or segment the data."
                        ),
                        confidence=0.8,
                        evidence=[
                            evidence(
                                f"dataset:{dataset}",
                                calculation="std / mean",
                                values={"std": row["std"], "mean": row["mean"]},
                            )
                        ],
                    )
                )

    return AnalysisResult(
        kind=AnalysisKind.DESCRIBE,
        dataset=dataset,
        summary=(
            f"{frame.height:,} rows × {frame.width} columns; "
            f"{len(numeric)} numeric and {len(categorical)} categorical column(s) summarised."
        ),
        metrics={
            "rows": frame.height,
            "columns": frame.width,
            "numeric_columns": len(numeric),
            "categorical_columns": len(categorical),
            "total_missing": int(sum(r["missing"] for r in summary_rows)),
        },
        tables={"summary": summary_rows, **category_tables},
        charts=charts,
        insights=insights,
        evidence=[evidence(f"dataset:{dataset}", rows=frame.height)],
        params={"columns": selected},
    )


def _histogram(series: pl.Series, bins: int = 20) -> tuple[list[int], list[float]]:
    import numpy as np

    array = series.cast(pl.Float64, strict=False).drop_nulls().to_numpy()
    counts, edges = np.histogram(array, bins=min(bins, max(len(array), 1)))
    return [int(c) for c in counts], [float(e) for e in edges]
