"""API dependencies: authentication, tenant resolution and the per-request service graph.

Every authenticated route depends on :func:`service_context`, which yields a
:class:`ServiceContext` bound to the caller's principal *and* commits (or rolls back) the
transaction when the request ends. A route therefore cannot forget to scope by tenant, and a
failed request cannot leave a half-written change behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from gdap.core.config import Settings
from gdap.core.container import Platform, get_platform
from gdap.core.contracts import Principal
from gdap.core.enums import Permission, Role
from gdap.core.errors import AuthenticationError, AuthorizationError
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger, log_context
from gdap.security import api_keys
from gdap.security.rbac import permissions_for
from gdap.storage.repositories import ApiKeyRepository, OrganizationRepository, UserRepository

log = get_logger(__name__)


def platform_dependency() -> Platform:
    return get_platform()


PlatformDep = Annotated[Platform, Depends(platform_dependency)]


def settings_dependency(platform: PlatformDep) -> Settings:
    return platform.settings


SettingsDep = Annotated[Settings, Depends(settings_dependency)]


def service_context(
    request: Request,
    platform: PlatformDep,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Iterator[ServiceContext]:
    """Authenticate, open one transaction, and expose the service graph for this request."""
    presented = api_key or _bearer(authorization)

    with platform.db.session() as session:
        principal = _resolve_principal(session, platform, presented)
        request.state.principal = principal
        with log_context(org_id=principal.org_id, actor=principal.user_id):
            yield platform.context(session, principal)


ContextDep = Annotated[ServiceContext, Depends(service_context)]


def require_admin(context: ContextDep) -> ServiceContext:
    if not context.principal.has(Permission.ADMIN):
        raise AuthorizationError("this endpoint requires an administrator")
    return context


AdminDep = Annotated[ServiceContext, Depends(require_admin)]


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


PaginationDep = Annotated[Pagination, Depends(Pagination)]


# ─────────────────────────────────────── authentication ────────────────────────────────────


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _resolve_principal(session: object, platform: Platform, presented: str | None) -> Principal:
    settings = platform.settings

    if not settings.security.auth_enabled:
        # Development and test mode: act as the default organisation's owner. Never reachable in
        # production, because config/production.yaml forces auth_enabled=true.
        return platform.resolve_principal(session)  # type: ignore[arg-type]

    if not presented:
        raise AuthenticationError(
            "missing API key",
            details={"header": settings.security.api_key_header, "hint": "gdap system key create"},
        )

    prefix, secret = api_keys.split(presented)
    if not prefix or not secret:
        raise AuthenticationError("malformed API key")

    repo = ApiKeyRepository(session, "*")  # type: ignore[arg-type]
    record = repo.by_prefix(prefix)
    if record is None or not api_keys.verify(presented, record.key_hash):
        log.warning("auth_failed", prefix=prefix)
        raise AuthenticationError("invalid API key")
    if record.expires_at and _aware(record.expires_at) < datetime.now(UTC):
        raise AuthenticationError("API key expired")

    user = UserRepository(session, record.org_id).get(record.user_id)  # type: ignore[arg-type]
    if user is None or not user.is_active:
        raise AuthenticationError("the user behind this key is inactive")

    organization = OrganizationRepository(session).get(record.org_id)  # type: ignore[arg-type]
    if organization is None or not organization.is_active:
        raise AuthenticationError("organization is inactive")

    record.last_used_at = datetime.now(UTC)

    role = Role(user.role)
    granted = permissions_for(role)
    scopes = set(record.scopes or [])
    if scopes:  # a key may hold *fewer* permissions than its user, never more
        granted = frozenset(p for p in granted if p.value in scopes)

    return Principal(
        org_id=record.org_id,
        user_id=user.id,
        email=user.email,
        role=role,
        permissions=granted,
        api_key_id=record.id,
    )


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
