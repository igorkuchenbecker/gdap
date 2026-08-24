"""Audit trail (§16).

Every state-changing operation is recorded with actor, action, resource, result and details.
Auditing failures never break the operation being audited — but they are logged loudly, because a
silent audit gap is a compliance incident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from gdap.core.contracts import AuditEvent, Principal
from gdap.observability.logging import current_context, get_logger
from gdap.security.secrets import SecretsResolver
from gdap.storage.repositories import AuditRepository

log = get_logger(__name__)


class AuditTrail:
    def __init__(self, session: Session, org_id: str) -> None:
        self.repo = AuditRepository(session, org_id)

    def record(
        self,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        *,
        result: str = "success",
        details: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        payload = SecretsResolver.redact(details or {})
        context = current_context()
        try:
            self.repo.append(
                actor=principal.user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result=result,
                details=payload,
                trace_id=context.get("trace_id"),
                ip=ip,
            )
        except Exception as exc:  # pragma: no cover - audit must not mask the real error
            log.error(
                "audit_write_failed",
                action=action,
                resource_type=resource_type,
                error=str(exc),
            )
            return
        log.info(
            "audit",
            actor=principal.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
        )

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        rows = self.repo.query(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            since=since,
            limit=limit,
            offset=offset,
        )
        return [
            AuditEvent(
                actor=row.actor,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                result=row.result,  # type: ignore[arg-type]
                details=row.details or {},
                at=row.at,
                trace_id=row.trace_id,
            )
            for row in rows
        ]
