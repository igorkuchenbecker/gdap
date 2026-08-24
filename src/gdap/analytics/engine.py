"""Analytics engine: one entry point, dispatching to the analysis implementations."""

from __future__ import annotations

from typing import Any

import polars as pl

from gdap.analytics import anomaly, descriptive, diagnostic, trend
from gdap.analytics.common import pick_metric, pick_time_column
from gdap.core.contracts import AnalysisResult
from gdap.core.enums import AnalysisKind
from gdap.core.errors import ValidationFailedError
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS

log = get_logger(__name__)


class AnalyticsEngine:
    """Stateless: every call takes the frame it operates on, which keeps it trivially poolable."""

    def run(
        self,
        kind: AnalysisKind | str,
        frame: pl.DataFrame,
        *,
        dataset: str,
        params: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        analysis_kind = AnalysisKind(kind)
        options = dict(params or {})
        if frame.is_empty():
            raise ValidationFailedError(f"dataset '{dataset}' has no rows to analyse")

        with METRICS.timer("analysis_ms", kind=analysis_kind.value):
            result = self._dispatch(analysis_kind, frame, dataset, options)
        METRICS.increment("analyses_total", kind=analysis_kind.value)
        log.info(
            "analysis_completed",
            kind=analysis_kind.value,
            dataset=dataset,
            insights=len(result.insights),
        )
        return result

    def _dispatch(
        self,
        kind: AnalysisKind,
        frame: pl.DataFrame,
        dataset: str,
        options: dict[str, Any],
    ) -> AnalysisResult:
        if kind is AnalysisKind.DESCRIBE:
            return descriptive.describe(frame, dataset=dataset, columns=options.get("columns"))

        if kind is AnalysisKind.CORRELATION:
            return diagnostic.correlate(
                frame,
                dataset=dataset,
                columns=options.get("columns"),
                threshold=float(options.get("threshold", 0.5)),
            )

        if kind is AnalysisKind.SEGMENTATION:
            dimension = options.get("dimension")
            if not dimension:
                raise ValidationFailedError("segmentation requires params.dimension")
            return diagnostic.segment(
                frame,
                dataset=dataset,
                metric=pick_metric(frame, options.get("metric")),
                dimension=str(dimension),
                agg=str(options.get("agg", "sum")),
                top=int(options.get("top", 20)),
            )

        if kind is AnalysisKind.COMPARISON:
            time_column = pick_time_column(frame, options.get("time_column"))
            if not time_column:
                raise ValidationFailedError("comparison requires a temporal column")
            return diagnostic.compare_periods(
                frame,
                dataset=dataset,
                metric=pick_metric(frame, options.get("metric")),
                time_column=time_column,
                dimension=options.get("dimension"),
                granularity=str(options.get("granularity", "month")),
            )

        if kind is AnalysisKind.DRIVERS:
            return diagnostic.drivers(
                frame,
                dataset=dataset,
                metric=pick_metric(frame, options.get("metric")),
                dimensions=options.get("dimensions"),
            )

        if kind is AnalysisKind.TREND:
            time_column = pick_time_column(frame, options.get("time_column"))
            if not time_column:
                raise ValidationFailedError("trend analysis requires a temporal column")
            return trend.analyse_trend(
                frame,
                dataset=dataset,
                metric=pick_metric(frame, options.get("metric")),
                time_column=time_column,
                granularity=str(options.get("granularity", "month")),
                agg=str(options.get("agg", "sum")),
            )

        if kind is AnalysisKind.FORECAST:
            time_column = pick_time_column(frame, options.get("time_column"))
            if not time_column:
                raise ValidationFailedError("forecasting requires a temporal column")
            return trend.forecast(
                frame,
                dataset=dataset,
                metric=pick_metric(frame, options.get("metric")),
                time_column=time_column,
                granularity=str(options.get("granularity", "month")),
                horizon=int(options.get("horizon", 3)),
                agg=str(options.get("agg", "sum")),
                seasonality=options.get("seasonality"),
            )

        if kind is AnalysisKind.ANOMALY:
            return anomaly.detect(
                frame,
                dataset=dataset,
                columns=options.get("columns"),
                method=options.get("method", "auto"),
                time_column=pick_time_column(frame, options.get("time_column"))
                if options.get("method") in (None, "auto", "timeseries")
                else None,
                metric=options.get("metric"),
                granularity=str(options.get("granularity", "day")),
                threshold=float(options.get("threshold", 3.0)),
            )

        raise ValidationFailedError(f"analysis kind '{kind}' is not implemented")
