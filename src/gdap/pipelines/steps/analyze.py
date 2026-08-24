"""Analysis steps: run any analytics kind against the working frame."""

from __future__ import annotations

from gdap.analytics.engine import AnalyticsEngine
from gdap.core.contracts import StepSpec
from gdap.core.enums import AnalysisKind
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step


def _run(context: StepContext, step: StepSpec, kind: AnalysisKind) -> StepOutcome:
    frame = context.frame(step.input)
    # The analysis is about the frame it reads: an explicit label wins, then the step's input
    # frame, and only then whatever the previous step happened to publish.
    dataset = str(context.option(step, "dataset") or step.input or context.current or "data")
    params = {
        key: value for key, value in step.with_.items() if key not in {"dataset", "kind", "persist"}
    }
    persist = bool(context.option(step, "persist", True)) and not context.dry_run

    # An analysis over a catalogued dataset is recorded: lineage, audit, dashboard and
    # reproducibility all depend on it. An analysis over an intermediate in-pipeline frame has no
    # dataset version to attach to, so it runs through the engine and lives only in the job result.
    catalogued = context.services.datasets.repo.by_name(dataset) is not None
    if catalogued:
        result = context.services.analyses.run(
            dataset,
            kind,
            params=params,
            frame=frame,
            job_id=context.job_id,
            persist=persist,
        )
    else:
        result = AnalyticsEngine().run(kind, frame, dataset=dataset, params=params)
    context.analyses.append(result)
    context.insights.extend(result.insights)
    for key, value in result.metrics.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            context.metrics[f"{kind.value}_{key}"] = value
    return StepOutcome(
        frame=frame,
        message=result.summary,
        metrics={
            "insights": len(result.insights),
            **{
                key: value
                for key, value in result.metrics.items()
                if isinstance(value, int | float | str)
            },
        },
        insights=result.insights,
    )


@register_step(
    "analyze",
    description="Run an analysis by kind (describe, trend, anomaly, segmentation, …).",
    category="analytics",
    options={
        "kind": "describe|correlation|segmentation|comparison|drivers|trend|forecast|anomaly",
        "metric": "metric column",
        "dimension": "grouping column",
        "time_column": "temporal column",
        "granularity": "day|week|month|quarter|year",
    },
)
def analyze(context: StepContext, step: StepSpec) -> StepOutcome:
    kind = AnalysisKind(str(context.option(step, "kind", "describe")))
    return _run(context, step, kind)


def _alias(key: str, kind: AnalysisKind, description: str) -> None:
    @register_step(key, description=description, category="analytics")
    def _handler(context: StepContext, step: StepSpec, _kind: AnalysisKind = kind) -> StepOutcome:
        return _run(context, step, _kind)


_alias("analyze.describe", AnalysisKind.DESCRIBE, "Descriptive statistics for every column.")
_alias("analyze.correlation", AnalysisKind.CORRELATION, "Correlation matrix over numeric columns.")
_alias("analyze.segmentation", AnalysisKind.SEGMENTATION, "Break a metric down by a dimension.")
_alias(
    "analyze.comparison",
    AnalysisKind.COMPARISON,
    "Compare the latest period with the previous one.",
)
_alias("analyze.drivers", AnalysisKind.DRIVERS, "Rank dimensions by explained variance.")
_alias("analyze.trend", AnalysisKind.TREND, "Trend, growth rate and moving average over time.")
_alias("analyze.forecast", AnalysisKind.FORECAST, "Project the metric forward with an interval.")
_alias("analyze.anomaly", AnalysisKind.ANOMALY, "Detect anomalous values, periods or rows.")
