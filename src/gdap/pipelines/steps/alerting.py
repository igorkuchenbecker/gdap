"""Alerting steps: raise alerts from pipeline metrics and thresholds (§22)."""

from __future__ import annotations

from gdap.core.contracts import StepSpec
from gdap.core.enums import Severity
from gdap.core.errors import ValidationFailedError
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step

_OPERATORS = {
    "gt": lambda value, threshold: value > threshold,
    "gte": lambda value, threshold: value >= threshold,
    "lt": lambda value, threshold: value < threshold,
    "lte": lambda value, threshold: value <= threshold,
    "eq": lambda value, threshold: value == threshold,
    "ne": lambda value, threshold: value != threshold,
}


@register_step(
    "alert.threshold",
    description="Raise an alert when a pipeline metric crosses a threshold.",
    read_only=False,
    category="alerting",
    options={
        "metric": "metric name recorded by an earlier step (required)",
        "operator": "gt|gte|lt|lte|eq|ne (default lt)",
        "threshold": "numeric threshold (required)",
        "severity": "info|warning|critical (default warning)",
        "title": "alert title",
        "message": "alert message",
        "channels": "delivery channels",
    },
)
def alert_threshold(context: StepContext, step: StepSpec) -> StepOutcome:
    metric = str(context.option(step, "metric", required=True))
    threshold = float(context.option(step, "threshold", required=True))
    operator = str(context.option(step, "operator", "lt"))
    comparison = _OPERATORS.get(operator)
    if comparison is None:
        raise ValidationFailedError(
            f"unknown operator '{operator}'", details={"supported": sorted(_OPERATORS)}
        )

    raw = context.metrics.get(metric)
    if raw is None:
        return StepOutcome(
            message=f"metric '{metric}' not recorded by this run — nothing to evaluate",
            metrics={"evaluated": False},
        )

    value = float(raw)
    triggered = bool(comparison(value, threshold))
    if not triggered:
        return StepOutcome(
            message=f"{metric}={value:g} did not cross {operator} {threshold:g}",
            metrics={"evaluated": True, "triggered": False, metric: value},
        )

    severity = Severity(str(context.option(step, "severity", "warning")))
    title = str(
        context.option(step, "title", f"{metric} {operator} {threshold:g} (actual {value:g})")
    )
    alert = context.services.alerts.raise_alert(
        rule=f"pipeline:{context.pipeline}:{metric}",
        severity=severity,
        title=title,
        message=str(context.option(step, "message", f"Raised by pipeline '{context.pipeline}'.")),
        payload={
            "pipeline": context.pipeline,
            "job_id": context.job_id,
            "metric": metric,
            "value": value,
            "threshold": threshold,
        },
        channels=list(context.option(step, "channels", ["log", "store"]) or ["log", "store"]),
        dedupe_key=f"pipeline:{context.pipeline}:{metric}",
    )
    return StepOutcome(
        message=f"alert raised: {title}" + ("" if alert else " (suppressed as duplicate)"),
        metrics={"evaluated": True, "triggered": True, "alert_id": alert.id if alert else None},
    )


@register_step(
    "alert.raise",
    description="Raise an alert unconditionally (useful after a manual condition step).",
    read_only=False,
    category="alerting",
    options={"title": "required", "message": "required", "severity": "info|warning|critical"},
)
def alert_raise(context: StepContext, step: StepSpec) -> StepOutcome:
    alert = context.services.alerts.raise_alert(
        rule=f"pipeline:{context.pipeline}",
        severity=Severity(str(context.option(step, "severity", "info"))),
        title=str(context.option(step, "title", required=True)),
        message=str(context.option(step, "message", required=True)),
        payload={"pipeline": context.pipeline, "job_id": context.job_id, **context.metrics},
        channels=list(context.option(step, "channels", ["log", "store"]) or ["log", "store"]),
    )
    return StepOutcome(
        message="alert raised" if alert else "alert suppressed as duplicate",
        metrics={"alert_id": alert.id if alert else None},
    )
