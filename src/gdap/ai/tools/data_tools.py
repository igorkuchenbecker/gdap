"""Built-in agent tools.

Each tool is a thin, audited wrapper over a platform service — never over raw storage. That is
what makes the AI layer safe by construction: RBAC, tenant scoping, SQL policy, masking and
lineage all apply because the tool goes through the same service the API uses.

Every tool returns ``(content, evidence)`` so a claim in an answer can be traced to the query or
calculation that produced it (§12).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from gdap.ai.tools.registry import ToolContext, tool
from gdap.core.contracts import ChartSpec, Evidence
from gdap.core.enums import AnalysisKind, ApprovalMode, Permission, ReportFormat, Severity
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import to_records
from gdap.security.sql_guard import SqlPolicy

_DATASET_PARAM = {"dataset": {"type": "string", "description": "dataset name"}}
_METRIC_PARAM = {"metric": {"type": "string", "description": "numeric column to measure"}}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


# ────────────────────────────────────────── discovery ──────────────────────────────────────


@tool(
    "list_datasets",
    "List the datasets available in this workspace with their size and quality score.",
    parameters=_schema({"limit": {"type": "integer", "description": "max datasets (default 25)"}}),
    permissions=[Permission.DATASET_READ],
    category="discovery",
)
def list_datasets(context: ToolContext, limit: int = 25) -> tuple[Any, Evidence]:
    rows = context.services.datasets.list(limit=int(limit))
    content = [
        {
            "dataset": row.name,
            "rows": row.row_count,
            "versions": row.current_version,
            "quality_score": row.quality_score,
            "classification": row.classification,
            "description": row.description,
        }
        for row in rows
    ]
    return content, Evidence(
        source="catalog", calculation="datasets.list()", rows_considered=len(rows)
    )


@tool(
    "describe_dataset",
    "Return a dataset's schema, row count, column meanings and latest quality score.",
    parameters=_schema(_DATASET_PARAM),
    permissions=[Permission.DATASET_READ],
    category="discovery",
)
def describe_dataset(context: ToolContext, dataset: str | None = None) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    row = context.services.datasets.get(name)
    schema = context.services.datasets.schema(name)
    content = {
        "dataset": row.name,
        "rows": row.row_count,
        "version": row.current_version,
        "classification": row.classification,
        "quality_score": row.quality_score,
        "columns": [
            {
                "name": column.name,
                "type": column.dtype,
                "meaning": column.semantic_type.value,
                "nullable": column.nullable,
                "classification": column.classification.value,
            }
            for column in schema.columns
        ],
    }
    return content, Evidence(
        source=f"dataset:{row.name}",
        calculation="schema of the latest version",
        values={"version": row.current_version},
        rows_considered=row.row_count,
    )


@tool(
    "profile_dataset",
    "Profile a dataset: distributions, missing values, duplicates, keys and recommendations.",
    parameters=_schema(_DATASET_PARAM),
    permissions=[Permission.DATASET_READ],
    category="discovery",
)
def profile_dataset(context: ToolContext, dataset: str | None = None) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    profile = context.services.datasets.profile(name, persist=False)
    content = {
        "dataset": name,
        "rows": profile.rows,
        "columns": profile.columns,
        "duplicate_rows": profile.duplicate_rows,
        "candidate_keys": profile.candidate_keys,
        "recommendations": profile.recommendations[:8],
        "column_summary": [
            {
                "column": column.name,
                "meaning": column.semantic_type.value,
                "missing_pct": round(column.null_ratio * 100, 2),
                "distinct": column.distinct_count,
                "mean": column.numeric.mean if column.numeric else None,
                "min": column.numeric.min if column.numeric else None,
                "max": column.numeric.max if column.numeric else None,
            }
            for column in profile.column_profiles
        ],
    }
    return content, Evidence(
        source=f"dataset:{name}", calculation="full profile", rows_considered=profile.rows
    )


@tool(
    "quality_report",
    "Evaluate data quality across seven dimensions and list the findings.",
    parameters=_schema(_DATASET_PARAM),
    permissions=[Permission.DATASET_READ],
    category="quality",
)
def quality_report(context: ToolContext, dataset: str | None = None) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    report = context.services.datasets.validate(name, auto_expectations=True, persist=False)
    content = {
        "dataset": name,
        "score": report.score,
        "status": report.status,
        "dimensions": {d.dimension.value: d.score for d in report.dimensions},
        "findings": [
            {
                "severity": finding.severity.value,
                "rule": finding.rule,
                "column": finding.column,
                "message": finding.message,
                "failed_rows": finding.failed_rows,
            }
            for finding in report.findings[:15]
        ],
    }
    return content, Evidence(
        source=f"dataset:{name}",
        calculation="weighted quality score over seven dimensions",
        values={"score": report.score, "status": report.status},
        rows_considered=report.rows_checked,
    )


# ─────────────────────────────────────────── querying ──────────────────────────────────────


@tool(
    "run_sql",
    "Run a read-only SQL SELECT over the workspace datasets (each dataset is a table).",
    parameters=_schema(
        {
            "sql": {"type": "string", "description": "a single SELECT statement"},
            "limit": {"type": "integer", "description": "max rows (default 200)"},
        },
        ["sql"],
    ),
    permissions=[Permission.DATASET_READ],
    category="query",
)
def run_sql(context: ToolContext, sql: str, limit: int = 200) -> tuple[Any, Evidence]:
    policy = SqlPolicy.agent()
    capped = min(int(limit), policy.max_rows, context.max_rows)
    result = context.services.datasets.query(sql, limit=capped)
    return (
        {
            "columns": result["columns"],
            "rows": result["rows"],
            "records": result["records"][:capped],
        },
        Evidence(
            source="warehouse",
            query=sql,
            values={"row_limit": capped},
            rows_considered=result["rows"],
        ),
    )


@tool(
    "calculate_metric",
    "Aggregate a numeric column, optionally grouped by a dimension and filtered.",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "aggregation": {
                "type": "string",
                "enum": ["sum", "mean", "median", "min", "max", "count"],
            },
            "group_by": {"type": "string", "description": "optional dimension column"},
            "where": {"type": "string", "description": "optional safe filter expression"},
        },
    ),
    permissions=[Permission.DATASET_READ],
    category="query",
)
def calculate_metric(
    context: ToolContext,
    metric: str | None = None,
    dataset: str | None = None,
    aggregation: str = "sum",
    group_by: str | None = None,
    where: str | None = None,
) -> tuple[Any, Evidence]:
    from gdap.pipelines.expressions import parse_expression

    name = context.default_dataset(dataset)
    metric = metric or context.hints(name).get("metric")
    frame = context.services.datasets.frame(name)
    if not metric or metric not in frame.columns:
        raise ValidationFailedError(
            f"column '{metric}' not found in '{name}'", details={"available": frame.columns}
        )
    if where:
        frame = frame.filter(parse_expression(where, columns=frame.columns))

    expression = {
        "sum": pl.col(metric).sum(),
        "mean": pl.col(metric).mean(),
        "median": pl.col(metric).median(),
        "min": pl.col(metric).min(),
        "max": pl.col(metric).max(),
        "count": pl.col(metric).count(),
    }.get(aggregation)
    if expression is None:
        raise ValidationFailedError(f"unsupported aggregation '{aggregation}'")

    if group_by:
        if group_by not in frame.columns:
            raise ValidationFailedError(
                f"column '{group_by}' not found", details={"available": frame.columns}
            )
        grouped = (
            frame.group_by(group_by)
            .agg(expression.alias(aggregation), pl.len().alias("rows"))
            .sort(aggregation, descending=True)
            .head(context.max_rows)
        )
        content: Any = to_records(grouped)
    else:
        value = frame.select(expression.alias(aggregation)).item()
        content = {aggregation: value, "rows": frame.height}

    return content, Evidence(
        source=f"dataset:{name}",
        query=f"SELECT {aggregation}({metric}){f' GROUP BY {group_by}' if group_by else ''}"
        + (f" WHERE {where}" if where else ""),
        calculation=f"{aggregation}({metric})",
        rows_considered=frame.height,
    )


# ─────────────────────────────────────────── analytics ─────────────────────────────────────


def _run_analysis(
    context: ToolContext, kind: AnalysisKind, dataset: str | None, params: dict[str, Any]
) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    hints = context.hints(name)
    resolved = dict(params)
    inferred: list[str] = []
    for key in ("metric", "dimension", "time_column"):
        if key in resolved and resolved[key] is None and hints.get(key):
            resolved[key] = hints[key]
            inferred.append(f"{key}={hints[key]}")
    result = context.services.analyses.run(name, kind, params=resolved, persist=False)
    content = {
        "summary": result.summary,
        "metrics": result.metrics,
        "insights": [
            {
                "kind": insight.kind.value,
                "title": insight.title,
                "detail": insight.detail,
                "severity": insight.severity.value,
                "confidence": insight.confidence,
            }
            for insight in result.insights
        ],
        "table": next(iter(result.tables.values()), [])[:10],
    }
    if inferred:
        content["inferred_arguments"] = inferred
    evidence = Evidence(
        source=f"dataset:{name}",
        calculation=f"{kind.value} analysis"
        + (f" (inferred {', '.join(inferred)})" if inferred else ""),
        values={k: v for k, v in result.metrics.items() if isinstance(v, int | float | str)},
    )
    return content, evidence


@tool(
    "analyze_trend",
    "Measure how a metric evolves over time (slope, growth, moving average).",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "time_column": {"type": "string"},
            "granularity": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
        }
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def analyze_trend(
    context: ToolContext,
    dataset: str | None = None,
    metric: str | None = None,
    time_column: str | None = None,
    granularity: str = "month",
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context,
        AnalysisKind.TREND,
        dataset,
        {"metric": metric, "time_column": time_column, "granularity": granularity},
    )


@tool(
    "compare_periods",
    "Compare the latest period against the previous one, overall and per dimension.",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "dimension": {"type": "string"},
            "time_column": {"type": "string"},
            "granularity": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
        }
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def compare_periods(
    context: ToolContext,
    dataset: str | None = None,
    metric: str | None = None,
    dimension: str | None = None,
    time_column: str | None = None,
    granularity: str = "month",
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context,
        AnalysisKind.COMPARISON,
        dataset,
        {
            "metric": metric,
            "dimension": dimension,
            "time_column": time_column,
            "granularity": granularity,
        },
    )


@tool(
    "segment_metric",
    "Break a metric down by a dimension and rank the segments.",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "dimension": {"type": "string"},
            "aggregation": {"type": "string"},
        },
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def segment_metric(
    context: ToolContext,
    dimension: str | None = None,
    dataset: str | None = None,
    metric: str | None = None,
    aggregation: str = "sum",
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context,
        AnalysisKind.SEGMENTATION,
        dataset,
        {"metric": metric, "dimension": dimension, "agg": aggregation},
    )


@tool(
    "find_drivers",
    "Rank which dimensions explain most of a metric's variance (association, not causation).",
    parameters=_schema({**_DATASET_PARAM, **_METRIC_PARAM}),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def find_drivers(
    context: ToolContext, dataset: str | None = None, metric: str | None = None
) -> tuple[Any, Evidence]:
    return _run_analysis(context, AnalysisKind.DRIVERS, dataset, {"metric": metric})


@tool(
    "detect_anomaly",
    "Detect anomalous values, periods or rows in a dataset.",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "method": {
                "type": "string",
                "enum": ["auto", "zscore", "iqr", "timeseries", "isolation"],
            },
            "time_column": {"type": "string"},
            "granularity": {"type": "string"},
        }
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def detect_anomaly(
    context: ToolContext,
    dataset: str | None = None,
    metric: str | None = None,
    method: str = "auto",
    time_column: str | None = None,
    granularity: str = "week",
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context,
        AnalysisKind.ANOMALY,
        dataset,
        {
            "metric": metric,
            "method": method,
            "time_column": time_column,
            "granularity": granularity,
        },
    )


@tool(
    "forecast_metric",
    "Project a metric forward with an 80% prediction interval.",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "time_column": {"type": "string"},
            "granularity": {"type": "string"},
            "horizon": {"type": "integer", "description": "periods ahead (default 3)"},
        }
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def forecast_metric(
    context: ToolContext,
    dataset: str | None = None,
    metric: str | None = None,
    time_column: str | None = None,
    granularity: str = "month",
    horizon: int = 3,
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context,
        AnalysisKind.FORECAST,
        dataset,
        {
            "metric": metric,
            "time_column": time_column,
            "granularity": granularity,
            "horizon": int(horizon),
        },
    )


@tool(
    "correlate_columns",
    "Compute correlations between numeric columns.",
    parameters=_schema({**_DATASET_PARAM, "threshold": {"type": "number"}}),
    permissions=[Permission.ANALYSIS_RUN],
    category="analytics",
)
def correlate_columns(
    context: ToolContext, dataset: str | None = None, threshold: float = 0.5
) -> tuple[Any, Evidence]:
    return _run_analysis(
        context, AnalysisKind.CORRELATION, dataset, {"threshold": float(threshold)}
    )


# ─────────────────────────────────────── presentation ──────────────────────────────────────


@tool(
    "generate_chart",
    "Build a chart specification from a metric and dimension (rendered by the client).",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            **_METRIC_PARAM,
            "dimension": {"type": "string"},
            "kind": {"type": "string", "enum": ["bar", "hbar", "line", "pie", "scatter"]},
            "aggregation": {"type": "string"},
        },
    ),
    permissions=[Permission.ANALYSIS_RUN],
    category="presentation",
)
def generate_chart(
    context: ToolContext,
    dimension: str | None = None,
    dataset: str | None = None,
    metric: str | None = None,
    kind: str = "hbar",
    aggregation: str = "sum",
) -> tuple[Any, Evidence]:
    from gdap.analytics.common import pick_metric

    name = context.default_dataset(dataset)
    hints = context.hints(name)
    dimension = dimension or hints.get("dimension")
    metric = metric or hints.get("metric")
    frame = context.services.datasets.frame(name)
    measure = pick_metric(frame, metric)
    if not dimension or dimension not in frame.columns:
        raise ValidationFailedError(
            f"column '{dimension}' not found", details={"available": frame.columns}
        )
    grouped = (
        frame.group_by(dimension)
        .agg(getattr(pl.col(measure), aggregation)().alias("value"))
        .sort("value", descending=True)
        .head(25)
    )
    chart = ChartSpec(
        kind=kind,  # type: ignore[arg-type]
        title=f"{aggregation}({measure}) by {dimension}",
        x="value",
        y=dimension,
        data=to_records(grouped),
    )
    return {"chart": chart.model_dump(mode="json")}, Evidence(
        source=f"dataset:{name}",
        query=f"SELECT {dimension}, {aggregation}({measure}) GROUP BY {dimension}",
        rows_considered=frame.height,
    )


@tool(
    "create_report",
    "Render a report artifact for a dataset (profile, quality and analyses).",
    parameters=_schema(
        {
            **_DATASET_PARAM,
            "title": {"type": "string"},
            "formats": {"type": "array", "items": {"type": "string"}},
        }
    ),
    permissions=[Permission.REPORT_WRITE],
    approval=ApprovalMode.AUTO_WITH_VALIDATION,
    read_only=False,
    category="presentation",
)
def create_report(
    context: ToolContext,
    dataset: str | None = None,
    title: str | None = None,
    formats: list[str] | None = None,
) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    reports, spec = context.services.reports.dataset_report(
        name,
        title=title,
        formats=[ReportFormat(f) for f in (formats or ["html"])],
        job_id=context.job_id,
    )
    return (
        {
            "reports": [
                {
                    "id": row.id,
                    "format": row.format,
                    "download_url": f"/api/v1/reports/{row.id}/download",
                }
                for row in reports
            ],
            "executive_summary": spec.executive_summary,
        },
        Evidence(source=f"dataset:{name}", calculation="report generation"),
    )


# ─────────────────────────────────────── operations ────────────────────────────────────────


@tool(
    "get_lineage",
    "Show where a dataset came from and what was produced from it.",
    parameters=_schema({**_DATASET_PARAM, "depth": {"type": "integer"}}),
    permissions=[Permission.GOVERNANCE_READ],
    category="governance",
)
def get_lineage(
    context: ToolContext, dataset: str | None = None, depth: int = 2
) -> tuple[Any, Evidence]:
    name = context.default_dataset(dataset)
    row = context.services.datasets.get(name)
    graph = context.services.governance.lineage("dataset", row.id, depth=int(depth))
    return graph, Evidence(source=f"dataset:{name}", calculation="lineage graph traversal")


@tool(
    "send_alert",
    "Raise an alert to the configured channels. Outward-facing: needs human approval.",
    parameters=_schema(
        {
            "title": {"type": "string"},
            "message": {"type": "string"},
            "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        },
        ["title", "message"],
    ),
    permissions=[Permission.AGENT_USE],
    approval=ApprovalMode.REQUIRES_APPROVAL,
    read_only=False,
    category="operations",
)
def send_alert(
    context: ToolContext, title: str, message: str, severity: str = "warning"
) -> tuple[Any, Evidence]:
    alert = context.services.alerts.raise_alert(
        rule="agent",
        severity=Severity(severity),
        title=title,
        message=message,
        payload={"raised_by": "agent", "job_id": context.job_id},
    )
    return {"alert_id": alert.id if alert else None, "suppressed": alert is None}, Evidence(
        source="alerting", calculation="agent-raised alert"
    )


@tool(
    "schedule_pipeline",
    "Queue an existing pipeline for execution. Changes system state: needs human approval.",
    parameters=_schema(
        {"pipeline": {"type": "string"}, "params": {"type": "object"}}, ["pipeline"]
    ),
    permissions=[Permission.PIPELINE_RUN],
    approval=ApprovalMode.REQUIRES_APPROVAL,
    read_only=False,
    category="operations",
)
def schedule_pipeline(
    context: ToolContext, pipeline: str, params: dict[str, Any] | None = None
) -> tuple[Any, Evidence]:
    from gdap.core.enums import TriggerType

    job = context.services.pipelines.run(pipeline, params=params or {}, trigger=TriggerType.AGENT)
    return {"job_id": job.id, "state": job.state, "pipeline": pipeline}, Evidence(
        source="orchestrator", calculation="pipeline run queued by an agent"
    )
