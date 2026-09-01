"""Composition root.

The single place where abstractions are bound to implementations. Everything else receives what
it needs through constructor arguments, which is what makes the platform testable (swap SQLite for
Postgres, local storage for object storage, heuristic AI for a real LLM — all here).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from gdap.core.config import Settings, get_settings
from gdap.core.contracts import Principal
from gdap.core.enums import Role
from gdap.observability.logging import configure_logging, get_logger
from gdap.security.rbac import permissions_for
from gdap.security.secrets import SecretsResolver
from gdap.storage.backends import LocalFileStorage
from gdap.storage.database import Database
from gdap.storage.repositories import OrganizationRepository, UserRepository
from gdap.storage.warehouse import Warehouse

if TYPE_CHECKING:  # pragma: no cover
    from gdap.connectors.registry import ConnectorRegistry
    from gdap.core.services.context import ServiceContext

log = get_logger(__name__)


class Platform:
    """Owns process-wide resources: configuration, database, storage, registries."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        configure_logging(
            level=self.settings.observability.log_level,
            fmt=self.settings.observability.log_format,
            log_file=self.settings.observability.log_file,
        )
        self._db: Database | None = None

    # ------------------------------------------------------------------ resources
    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database.from_settings(self.settings)
        return self._db

    @cached_property
    def storage(self) -> LocalFileStorage:
        return LocalFileStorage(self.settings.paths.warehouse)  # type: ignore[arg-type]

    @cached_property
    def artifacts(self) -> LocalFileStorage:
        return LocalFileStorage(self.settings.paths.artifacts)  # type: ignore[arg-type]

    @cached_property
    def models_storage(self) -> LocalFileStorage:
        return LocalFileStorage(self.settings.paths.models)  # type: ignore[arg-type]

    @cached_property
    def staging(self) -> LocalFileStorage:
        return LocalFileStorage(self.settings.paths.staging)  # type: ignore[arg-type]

    @cached_property
    def warehouse(self) -> Warehouse:
        return Warehouse(self.storage)

    @cached_property
    def secrets(self) -> SecretsResolver:
        return SecretsResolver(self.settings)

    @cached_property
    def registry(self) -> ConnectorRegistry:
        from gdap.connectors.registry import get_registry

        return get_registry()

    # ------------------------------------------------------------------ lifecycle
    def bootstrap(self, *, org_slug: str | None = None, org_name: str | None = None) -> Principal:
        """Create the schema and the default tenant/owner. Idempotent."""
        self.db.create_all()
        slug = org_slug or self.settings.default_org_slug
        with self.db.session() as session:
            organizations = OrganizationRepository(session)
            org = organizations.by_slug(slug) or organizations.create(
                slug=slug, name=org_name or slug.title()
            )
            users = UserRepository(session, org.id)
            owner = users.by_email(f"owner@{slug}.local") or users.create(
                email=f"owner@{slug}.local",
                name="Platform Owner",
                role=Role.OWNER.value,
            )
            principal = _principal_for(org.id, owner.id, owner.email, Role(owner.role))
        log.info("platform_bootstrapped", org=slug, environment=self.settings.environment)
        return principal

    def system_principal(self, org_id: str, reason: str = "system") -> Principal:
        return Principal.system(org_id, reason)

    def resolve_principal(self, session: Session, *, org_slug: str | None = None) -> Principal:
        """Owner principal for CLI/local use. The API resolves principals from API keys instead."""
        slug = org_slug or self.settings.default_org_slug
        organizations = OrganizationRepository(session)
        org = organizations.by_slug(slug)
        if org is None:
            from gdap.core.errors import NotFoundError

            raise NotFoundError(
                f"organization '{slug}' does not exist — run 'gdap system init' first"
            )
        users = UserRepository(session, org.id)
        candidates = users.list(limit=1)
        owner = users.by_email(f"owner@{slug}.local") or (candidates[0] if candidates else None)
        if owner is None:
            from gdap.core.errors import NotFoundError

            raise NotFoundError(f"organization '{slug}' has no users")
        return _principal_for(org.id, owner.id, owner.email, Role(owner.role))

    @contextmanager
    def unit_of_work(self, principal: Principal) -> Iterator[ServiceContext]:
        """One transaction + one service graph, scoped to a principal and their tenant."""
        from gdap.core.services.context import ServiceContext

        with self.db.session() as session:
            yield ServiceContext(platform=self, session=session, principal=principal)

    def context(self, session: Session, principal: Principal) -> ServiceContext:
        """Service graph over a caller-managed session (used by the worker)."""
        from gdap.core.services.context import ServiceContext

        return ServiceContext(platform=self, session=session, principal=principal)

    def shutdown(self) -> None:
        if self._db is not None:
            self._db.dispose()
            self._db = None


def _principal_for(org_id: str, user_id: str, email: str, role: Role) -> Principal:
    return Principal(
        org_id=org_id,
        user_id=user_id,
        email=email,
        role=role,
        permissions=permissions_for(role),
    )


_PLATFORM: Platform | None = None


def get_platform(settings: Settings | None = None) -> Platform:
    """Process-wide platform singleton (the API and the worker share one)."""
    global _PLATFORM
    if _PLATFORM is None or settings is not None:
        _PLATFORM = Platform(settings)
    return _PLATFORM


def reset_platform() -> None:
    global _PLATFORM
    if _PLATFORM is not None:
        _PLATFORM.shutdown()
    _PLATFORM = None
