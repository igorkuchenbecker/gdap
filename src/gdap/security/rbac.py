"""Role-based access control.

Roles are coarse and few on purpose; permissions are the fine-grained unit checked in code.
``require`` is the single choke point — an endpoint or service that forgets to call it is a review
finding, not a silent hole, because every write path in the service layer starts with it.
"""

from __future__ import annotations

from gdap.core.contracts import Principal
from gdap.core.enums import Permission, Role
from gdap.core.errors import AuthorizationError
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_READ = {
    Permission.SOURCE_READ,
    Permission.DATASET_READ,
    Permission.PIPELINE_READ,
    Permission.JOB_READ,
    Permission.REPORT_READ,
    Permission.GOVERNANCE_READ,
}

_ANALYST = _READ | {
    Permission.ANALYSIS_RUN,
    Permission.REPORT_WRITE,
    Permission.AGENT_USE,
    Permission.PIPELINE_RUN,
}

_ENGINEER = _ANALYST | {
    Permission.SOURCE_WRITE,
    Permission.DATASET_WRITE,
    Permission.PIPELINE_WRITE,
    Permission.JOB_WRITE,
    Permission.SQL_WRITE,
}

_ADMIN = _ENGINEER | {Permission.SQL_DESTRUCTIVE, Permission.ADMIN}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_READ),
    Role.ANALYST: frozenset(_ANALYST),
    Role.ENGINEER: frozenset(_ENGINEER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_ADMIN),
    Role.SERVICE: frozenset(_ENGINEER),
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    role = Role(role)
    return ROLE_PERMISSIONS.get(role, frozenset())


def require(principal: Principal, *permissions: Permission, resource: str | None = None) -> None:
    """Raise :class:`AuthorizationError` unless the principal holds every permission."""
    missing = [p for p in permissions if not principal.has(p)]
    if missing:
        log.warning(
            "authorization_denied",
            actor=principal.user_id,
            org_id=principal.org_id,
            missing=[p.value for p in missing],
            resource=resource,
        )
        raise AuthorizationError(
            f"missing permission(s): {', '.join(p.value for p in missing)}",
            details={
                "required": [p.value for p in permissions],
                "role": principal.role.value,
                "resource": resource,
            },
        )


def require_same_tenant(principal: Principal, org_id: str, *, resource: str = "resource") -> None:
    """Defence in depth: repositories filter by tenant, services also assert it."""
    if principal.org_id != org_id:
        log.error(
            "tenant_isolation_violation",
            actor=principal.user_id,
            principal_org=principal.org_id,
            resource_org=org_id,
            resource=resource,
        )
        raise AuthorizationError(f"{resource} belongs to another organization")
