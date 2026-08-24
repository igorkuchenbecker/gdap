"""Governance endpoints: catalog, lineage, audit, retention, classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from gdap.api.deps import ContextDep, PaginationDep

router = APIRouter(prefix="/api/v1", tags=["governance"])


@router.get("/catalog", summary="Data catalog with ownership and classification")
def catalog(context: ContextDep) -> dict[str, Any]:
    return context.governance.catalog()


@router.get("/lineage/{node_type}/{node_id}", summary="Lineage graph around a node")
def lineage(
    node_type: str,
    node_id: str,
    context: ContextDep,
    depth: int = Query(default=3, ge=1, le=6),
) -> dict[str, Any]:
    return context.governance.lineage(node_type, node_id, depth=depth)


@router.get("/audit", summary="Query the audit trail")
def audit(
    context: ContextDep,
    page: PaginationDep,
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    since_hours: int | None = Query(default=None, ge=1, le=8760),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
    events = context.governance.audit(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )
    return {"items": events, "count": len(events)}


@router.get("/retention/candidates", summary="Dataset versions past their retention window")
def retention(context: ContextDep) -> dict[str, Any]:
    items = context.governance.retention_candidates()
    return {"items": items, "count": len(items)}


@router.get("/classification", summary="Datasets grouped by classification level")
def classification(context: ContextDep) -> dict[str, Any]:
    return context.governance.classification_summary()
