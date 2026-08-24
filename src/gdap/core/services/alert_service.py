"""Alert service (§22): raise, deduplicate, deliver, acknowledge."""

from __future__ import annotations

import builtins
from typing import Any

from gdap.core.contracts import AlertSpec
from gdap.core.enums import Severity
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.observability.notifications import CHANNELS
from gdap.storage import models as m
from gdap.storage.repositories import AlertRepository, AlertRuleRepository

log = get_logger(__name__)


class AlertService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = AlertRepository(context.session, context.org_id)
        self.rules = AlertRuleRepository(context.session, context.org_id)

    def raise_alert(
        self,
        *,
        rule: str,
        severity: Severity,
        title: str,
        message: str,
        payload: dict[str, Any] | None = None,
        channels: list[str] | None = None,
        dedupe_key: str | None = None,
        dedupe_window_minutes: int = 60,
    ) -> m.Alert | None:
        """Create and deliver an alert. Returns ``None`` when suppressed as a duplicate."""
        if dedupe_key:
            duplicate = self.repo.find_open_duplicate(
                dedupe_key, within_minutes=dedupe_window_minutes
            )
            if duplicate is not None:
                log.debug("alert_suppressed", rule=rule, dedupe_key=dedupe_key)
                METRICS.increment("alerts_suppressed_total", rule=rule)
                return None

        spec = AlertSpec(
            rule=rule,
            severity=severity,
            title=title,
            message=message,
            payload=payload or {},
            channels=channels or ["log", "store"],
            dedupe_key=dedupe_key,
        )
        delivered: list[str] = []
        for name in spec.channels:
            channel = CHANNELS.get(name)
            if channel is None:
                log.warning("alert_channel_unknown", channel=name, available=CHANNELS.names())
                continue
            try:
                if channel.send(spec):
                    delivered.append(name)
            except Exception as exc:  # a channel must never break the caller
                log.error("alert_channel_error", channel=name, error=str(exc))

        row = self.repo.create(
            rule=rule,
            severity=severity.value,
            title=title,
            message=message,
            payload=spec.payload,
            dedupe_key=dedupe_key,
            delivered=delivered,
        )
        METRICS.increment("alerts_total", severity=severity.value, rule=rule)
        self.context.audit.record(
            self.context.principal,
            "alert.raise",
            "alert",
            row.id,
            details={"rule": rule, "severity": severity.value, "delivered": delivered},
        )
        return row

    def list(self, *, status: str | None = None, limit: int = 50) -> builtins.list[m.Alert]:
        return self.repo.list(limit=limit, status=status)

    def acknowledge(self, alert_id: str) -> m.Alert:
        from gdap.storage.repositories import utcnow

        row = self.repo.get_or_raise(alert_id)
        row.status = "acknowledged"
        row.acknowledged_at = utcnow()
        self.context.session.flush()
        self.context.audit.record(self.context.principal, "alert.acknowledge", "alert", row.id)
        return row

    def evaluate_threshold_rules(
        self, metrics: dict[str, float], *, context_payload: dict[str, Any] | None = None
    ) -> builtins.list[m.Alert]:
        """Fire configured KPI-threshold rules against a metric snapshot (§22)."""
        fired: list[m.Alert] = []
        for rule in self.rules.enabled_rules(kind="metric_threshold"):
            condition = rule.condition or {}
            metric = str(condition.get("metric", ""))
            if metric not in metrics:
                continue
            value = float(metrics[metric])
            operator = str(condition.get("operator", "gt"))
            threshold = float(condition.get("threshold", 0))
            if _compare(value, operator, threshold):
                alert = self.raise_alert(
                    rule=rule.name,
                    severity=Severity(rule.severity),
                    title=f"{metric} {operator} {threshold} (actual {value:g})",
                    message=str(condition.get("message", f"{metric} crossed its threshold")),
                    payload={
                        "metric": metric,
                        "value": value,
                        "threshold": threshold,
                        **(context_payload or {}),
                    },
                    channels=list(rule.channels or ["log", "store"]),
                    dedupe_key=f"rule:{rule.id}",
                )
                if alert:
                    fired.append(alert)
        return fired

    @staticmethod
    def to_dict(row: m.Alert) -> dict[str, Any]:
        return {
            "id": row.id,
            "rule": row.rule,
            "severity": row.severity,
            "title": row.title,
            "message": row.message,
            "payload": row.payload or {},
            "status": row.status,
            "delivered": row.delivered or [],
            "created_at": row.created_at.isoformat(),
            "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        }


def _compare(value: float, operator: str, threshold: float) -> bool:
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
        "ne": value != threshold,
    }.get(operator, False)
