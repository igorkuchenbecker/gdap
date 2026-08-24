"""Source service: register, probe, discover, ingest."""

from __future__ import annotations

import builtins
from typing import Any

from gdap.core.contracts import (
    ConnectionTestResult,
    DiscoveredObject,
    IngestionResult,
    SourceSpec,
)
from gdap.core.enums import DataClassification, Permission
from gdap.core.errors import ApprovalRequiredError, ConflictError
from gdap.core.services.context import ServiceContext
from gdap.ingestion.engine import IngestionEngine, IngestRequest
from gdap.observability.logging import get_logger
from gdap.security.rbac import require
from gdap.security.secrets import SecretsResolver
from gdap.storage import models as m
from gdap.storage.repositories import SourceRepository

log = get_logger(__name__)


class SourceService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = SourceRepository(context.session, context.org_id)

    # ------------------------------------------------------------------ CRUD
    def register(self, spec: SourceSpec) -> m.Source:
        require(self.context.principal, Permission.SOURCE_WRITE, resource="source")
        if self.repo.by_name(spec.name):
            raise ConflictError(f"source '{spec.name}' already exists")

        registry = self.context.platform.registry
        registry.validate_config(spec)  # fails fast on a bad config

        row = self.repo.create(
            name=spec.name,
            type=spec.type.value,
            connector=spec.connector,
            config=spec.config,
            secret_refs=spec.secret_refs,
            classification=spec.classification.value,
            description=spec.description,
            tags=spec.tags,
            created_by=self.context.principal.user_id,
        )
        self.context.audit.record(
            self.context.principal,
            "source.create",
            "source",
            row.id,
            details={"name": spec.name, "connector": spec.connector},
        )
        log.info("source_registered", name=spec.name, connector=spec.connector)
        return row

    def update(self, reference: str, **changes: Any) -> m.Source:
        require(self.context.principal, Permission.SOURCE_WRITE, resource="source")
        row = self.repo.require_by_name_or_id(reference)
        for key in ("config", "secret_refs", "description", "tags", "classification"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "source.update",
            "source",
            row.id,
            details={"fields": sorted(k for k in changes if changes[k] is not None)},
        )
        return row

    def delete(self, reference: str) -> None:
        row = self.repo.require_by_name_or_id(reference)
        decision = self.context.policy.decide(
            self.context.principal,
            "source.delete",
            classification=DataClassification(row.classification),
        )
        decision.enforce()
        if decision.needs_human:
            raise ApprovalRequiredError(
                f"deleting source '{row.name}' requires explicit human approval",
                details={
                    "reason": decision.reason,
                    "approval": decision.approval.value,
                    "hint": "confirm with an administrator, then retry with approval recorded",
                },
            )
        self.repo.delete(row.id)
        self.context.audit.record(
            self.context.principal, "source.delete", "source", row.id, details={"name": row.name}
        )

    def list(self, *, limit: int = 100, offset: int = 0) -> builtins.list[m.Source]:
        require(self.context.principal, Permission.SOURCE_READ, resource="source")
        return self.repo.list(limit=limit, offset=offset)

    def get(self, reference: str) -> m.Source:
        require(self.context.principal, Permission.SOURCE_READ, resource="source")
        return self.repo.require_by_name_or_id(reference)

    # ------------------------------------------------------------------ operations
    def test(self, reference: str) -> ConnectionTestResult:
        require(self.context.principal, Permission.SOURCE_READ, resource="source")
        row = self.repo.require_by_name_or_id(reference)
        connector = self._connector(row)
        try:
            result = connector.test()
        finally:
            connector.close()
        row.status = "healthy" if result.ok else "unreachable"
        from gdap.storage.repositories import utcnow

        row.last_tested_at = utcnow()
        self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "source.test",
            "source",
            row.id,
            result="success" if result.ok else "error",
            details={"latency_ms": round(result.latency_ms, 2), "message": result.message},
        )
        return result

    def discover(self, reference: str) -> builtins.list[DiscoveredObject]:
        require(self.context.principal, Permission.SOURCE_READ, resource="source")
        row = self.repo.require_by_name_or_id(reference)
        connector = self._connector(row)
        try:
            objects = connector.discover()
        finally:
            connector.close()
        self.context.audit.record(
            self.context.principal,
            "source.discover",
            "source",
            row.id,
            details={"objects": len(objects)},
        )
        return objects

    def ingest(self, request: IngestRequest, *, job_id: str | None = None) -> IngestionResult:
        require(self.context.principal, Permission.DATASET_WRITE, resource="dataset")
        engine = IngestionEngine(
            self.context.session,
            self.context.org_id,
            self.context.settings,
            self.context.platform.warehouse,
            registry=self.context.platform.registry,
            secrets=self.context.platform.secrets,
        )
        return engine.ingest(request, self.context.principal, job_id=job_id)

    # ------------------------------------------------------------------ helpers
    def _connector(self, row: m.Source) -> Any:
        spec = SourceSpec(
            name=row.name,
            type=row.type,  # type: ignore[arg-type]
            connector=row.connector,
            config=row.config or {},
            secret_refs=row.secret_refs or {},
            classification=DataClassification(row.classification),
        )
        secrets = self.context.platform.secrets.resolve_all(row.secret_refs or {})
        return self.context.platform.registry.create(spec, secrets)

    @staticmethod
    def to_dict(row: m.Source) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "type": row.type,
            "connector": row.connector,
            "config": SecretsResolver.redact(row.config or {}),
            "secret_refs": sorted((row.secret_refs or {}).keys()),
            "classification": row.classification,
            "description": row.description,
            "tags": row.tags or [],
            "status": row.status,
            "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
            "created_at": row.created_at.isoformat(),
        }
