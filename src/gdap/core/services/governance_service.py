"""Governance service: catalog, lineage, audit and retention (§16, §44, §46)."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from typing import Any

from gdap.core.enums import DataClassification, Permission
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.security.rbac import require
from gdap.storage.repositories import DatasetRepository, PipelineRepository, SourceRepository

log = get_logger(__name__)


class GovernanceService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context

    # ------------------------------------------------------------------ catalog
    def catalog(self, *, limit: int = 200) -> dict[str, Any]:
        require(self.context.principal, Permission.GOVERNANCE_READ, resource="catalog")
        datasets = DatasetRepository(self.context.session, self.context.org_id)
        sources = SourceRepository(self.context.session, self.context.org_id)
        pipelines = PipelineRepository(self.context.session, self.context.org_id)

        entries: builtins.list[dict[str, Any]] = []
        for dataset in datasets.list(limit=limit):
            latest = datasets.latest_version(dataset.id)
            entries.append(
                {
                    "dataset": dataset.name,
                    "id": dataset.id,
                    "owner": dataset.owner,
                    "classification": dataset.classification,
                    "rows": dataset.row_count,
                    "versions": dataset.current_version,
                    "quality_score": dataset.quality_score,
                    "schema_fingerprint": latest.schema_fingerprint if latest else None,
                    "updated_at": dataset.updated_at.isoformat(),
                    "tags": dataset.tags or [],
                }
            )
        by_classification: dict[str, int] = {}
        for entry in entries:
            level = str(entry["classification"])
            by_classification[level] = by_classification.get(level, 0) + 1
        return {
            "datasets": entries,
            "counts": {
                "datasets": len(entries),
                "sources": sources.count(),
                "pipelines": pipelines.count(),
                "by_classification": by_classification,
            },
        }

    # ------------------------------------------------------------------ lineage & audit
    def lineage(self, node_type: str, node_id: str, *, depth: int = 3) -> dict[str, Any]:
        require(self.context.principal, Permission.GOVERNANCE_READ, resource="lineage")
        return self.context.lineage.graph(node_type, node_id, depth=depth)

    def audit(self, **filters: Any) -> list[dict[str, Any]]:
        require(self.context.principal, Permission.GOVERNANCE_READ, resource="audit")
        events = self.context.audit.query(**filters)
        return [event.model_dump(mode="json") for event in events]

    # ------------------------------------------------------------------ retention
    def retention_candidates(self, *, default_days: int | None = None) -> list[dict[str, Any]]:
        """Dataset versions past their retention window. Reported, never auto-deleted (§38)."""
        require(self.context.principal, Permission.GOVERNANCE_READ, resource="retention")
        datasets = DatasetRepository(self.context.session, self.context.org_id)
        policy_default = default_days or self.context.policy.settings.default_retention_days
        candidates: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for dataset in datasets.list(limit=500):
            days = dataset.retention_days or policy_default
            if not days:
                continue
            cutoff = now - timedelta(days=int(days))
            for version in datasets.versions(dataset.id, limit=100):
                created = version.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created < cutoff and version.version != dataset.current_version:
                    candidates.append(
                        {
                            "dataset": dataset.name,
                            "version": version.version,
                            "created_at": created.isoformat(),
                            "retention_days": int(days),
                            "size_bytes": version.size_bytes,
                            "classification": dataset.classification,
                        }
                    )
        return candidates

    def classification_summary(self) -> dict[str, Any]:
        datasets = DatasetRepository(self.context.session, self.context.org_id)
        summary: dict[str, list[str]] = {level.value: [] for level in DataClassification}
        for dataset in datasets.list(limit=500):
            summary.setdefault(dataset.classification, []).append(dataset.name)
        return {level: names for level, names in summary.items() if names}
