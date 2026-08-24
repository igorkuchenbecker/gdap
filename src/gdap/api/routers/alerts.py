"""Alert endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import AlertAckBody
from gdap.core.services.alert_service import AlertService

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("", summary="List alerts")
def list_alerts(
    context: ContextDep,
    page: PaginationDep,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    rows = context.alerts.list(status=status_filter, limit=page.limit)
    return {"items": [AlertService.to_dict(row) for row in rows], "count": len(rows)}


@router.post("/{alert_id}/acknowledge", summary="Acknowledge an alert")
def acknowledge(alert_id: str, body: AlertAckBody, context: ContextDep) -> dict[str, Any]:
    return AlertService.to_dict(context.alerts.acknowledge(alert_id))
