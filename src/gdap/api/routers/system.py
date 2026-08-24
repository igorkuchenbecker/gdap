"""System endpoints: health, readiness, metrics, capabilities and API key administration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, status

from gdap import __version__
from gdap.api.deps import AdminDep, ContextDep, PlatformDep
from gdap.api.schemas import CreateApiKeyBody
from gdap.core.contracts import SystemHealth
from gdap.core.enums import Role
from gdap.observability.health import check_platform
from gdap.observability.metrics import METRICS
from gdap.security import api_keys
from gdap.security.rbac import permissions_for
from gdap.storage.repositories import ApiKeyRepository, UserRepository

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness and (optionally) deep health checks")
def health(platform: PlatformDep, deep: bool = Query(default=False)) -> SystemHealth:
    return check_platform(platform, deep=deep)


@router.get("/readyz", summary="Readiness probe")
def readyz(platform: PlatformDep) -> dict[str, Any]:
    result = check_platform(platform)
    return {
        "ready": result.ok,
        "version": result.version,
        "failed": [check.component for check in result.checks if not check.ok],
    }


@router.get("/metrics", summary="In-process metrics snapshot")
def metrics() -> dict[str, Any]:
    return METRICS.snapshot()


@router.get("/api/v1/system/info", summary="Platform capabilities")
def info(platform: PlatformDep) -> dict[str, Any]:
    from gdap.pipelines.steps import step_catalog

    settings = platform.settings
    return {
        "name": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "locale": {
            "default_locale": settings.locale.default_locale,
            "timezone": settings.locale.default_timezone,
            "currency": settings.locale.default_currency,
        },
        "capabilities": {
            "connectors": platform.registry.keys(),
            "steps": [step["key"] for step in step_catalog()],
            "report_formats": ["html", "markdown", "json", "csv", "xlsx", "pdf*"],
            "ai_provider": settings.ai.provider,
            "auth_enabled": settings.security.auth_enabled,
            "sql_write_enabled": settings.security.sql_write_enabled,
        },
    }


@router.get("/api/v1/system/connectors", summary="Connector catalogue with config schemas")
def connectors(platform: PlatformDep) -> dict[str, Any]:
    items = platform.registry.list()
    return {"items": items, "count": len(items)}


@router.get("/api/v1/system/doctor", summary="Full self-diagnostic")
def doctor(platform: PlatformDep) -> SystemHealth:
    return check_platform(platform, deep=True)


@router.get("/api/v1/system/dashboard", summary="Aggregate view for the home screen")
def dashboard(context: ContextDep) -> dict[str, Any]:
    catalog = context.governance.catalog()
    jobs = context.jobs.list(limit=10)
    alerts = context.alerts.list(limit=10, status="open")
    datasets = context.datasets.list(limit=100)
    scored = [row.quality_score for row in datasets if row.quality_score is not None]
    return {
        "counts": {
            **catalog["counts"],
            "jobs_recent": len(jobs),
            "alerts_open": len(alerts),
            "rows_total": sum(row.row_count for row in datasets),
        },
        "quality": {
            "average": round(sum(scored) / len(scored), 1) if scored else None,
            "worst": min(scored) if scored else None,
        },
        "recent_jobs": [
            {
                "id": job.id,
                "pipeline": job.pipeline_name,
                "state": job.state,
                "created_at": job.created_at.isoformat(),
                "duration_seconds": (job.metrics or {}).get("duration_seconds"),
            }
            for job in jobs
        ],
        "open_alerts": [
            {"id": alert.id, "severity": alert.severity, "title": alert.title} for alert in alerts
        ],
        "datasets": catalog["datasets"][:10],
    }


@router.post(
    "/api/v1/admin/api-keys",
    status_code=status.HTTP_201_CREATED,
    summary="Issue an API key (shown once)",
)
def create_api_key(body: CreateApiKeyBody, context: AdminDep) -> dict[str, Any]:
    users = UserRepository(context.session, context.org_id)
    email = body.user_email or context.principal.email or f"{body.name}@service.local"
    user = users.by_email(email) or users.create(email=email, name=body.name, role=body.role)
    # Sync the role only for a *named service account* distinct from the caller (see
    # gdap.cli.main.key_create for why a reused identity's role must not silently stay stale).
    # Never for the caller's own account: when `user_email` is omitted this endpoint defaults to
    # "issue myself a scoped-down key" (self-service), and that must never be able to change the
    # caller's own stored role as a side effect — a key can only ever narrow permissions, and an
    # admin issuing themselves a narrower key must not risk demoting their own account.
    if user.role != body.role and user.id != context.principal.user_id:
        user.role = body.role
    issued = api_keys.generate()
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    scopes = body.scopes or [p.value for p in permissions_for(Role(body.role))]
    record = ApiKeyRepository(context.session, context.org_id).create(
        user_id=user.id,
        name=body.name,
        prefix=issued.prefix,
        key_hash=issued.key_hash,
        scopes=scopes,
        expires_at=expires_at,
    )
    context.audit.record(
        context.principal,
        "apikey.create",
        "api_key",
        record.id,
        details={"name": body.name, "role": body.role, "scopes": len(scopes)},
    )
    return {
        "id": record.id,
        "name": record.name,
        "prefix": record.prefix,
        "api_key": issued.plaintext,  # returned exactly once
        "role": body.role,
        "scopes": scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "warning": "store this key now — it cannot be retrieved again",
    }


@router.get("/api/v1/admin/api-keys", summary="List API keys (never the secrets)")
def list_api_keys(context: AdminDep) -> dict[str, Any]:
    rows = ApiKeyRepository(context.session, context.org_id).list(limit=100)
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "prefix": row.prefix,
                "scopes": row.scopes or [],
                "created_at": row.created_at.isoformat(),
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "revoked": row.revoked_at is not None,
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.delete(
    "/api/v1/admin/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
def revoke_api_key(key_id: str, context: AdminDep) -> None:
    ApiKeyRepository(context.session, context.org_id).revoke(key_id)
    context.audit.record(context.principal, "apikey.revoke", "api_key", key_id)
