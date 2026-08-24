"""Dataset service: catalog, preview, profiling, validation, cleaning, querying, versioning."""

from __future__ import annotations

import builtins
from typing import Any

import polars as pl

from gdap.cleaning.engine import CleaningEngine
from gdap.core.contracts import (
    CleaningProposal,
    CleaningResult,
    DatasetProfile,
    DatasetSchema,
    Expectation,
    QualityReport,
)
from gdap.core.enums import ApprovalMode, DataClassification, Permission, Severity
from gdap.core.errors import ApprovalRequiredError, NotFoundError, ValidationFailedError
from gdap.core.frames import to_records
from gdap.core.services.context import ServiceContext
from gdap.governance.classification import classify_schema, dataset_classification
from gdap.observability.logging import get_logger
from gdap.profiling.profiler import DataProfiler
from gdap.profiling.semantics import enrich_schema_semantics
from gdap.quality.engine import QualityEngine, suggest_expectations
from gdap.security.masking import apply_masking
from gdap.security.rbac import require
from gdap.security.sql_guard import SqlPolicy
from gdap.storage import models as m
from gdap.storage.query import DuckDBEngine
from gdap.storage.repositories import (
    DatasetRepository,
    ProfileRepository,
    QualityRepository,
)

log = get_logger(__name__)


class DatasetService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = DatasetRepository(context.session, context.org_id)
        self.profiles = ProfileRepository(context.session, context.org_id)
        self.quality_reports = QualityRepository(context.session, context.org_id)
        self.warehouse = context.platform.warehouse

    # ------------------------------------------------------------------ catalog
    def list(self, *, limit: int = 100, offset: int = 0) -> builtins.list[m.Dataset]:
        require(self.context.principal, Permission.DATASET_READ, resource="dataset")
        return self.repo.list(limit=limit, offset=offset)

    def get(self, reference: str) -> m.Dataset:
        require(self.context.principal, Permission.DATASET_READ, resource="dataset")
        return self.repo.require_by_name_or_id(reference)

    def versions(self, reference: str) -> builtins.list[m.DatasetVersion]:
        dataset = self.get(reference)
        return self.repo.versions(dataset.id)

    def schema(self, reference: str, version: int | None = None) -> DatasetSchema:
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        return DatasetSchema.model_validate(row.schema_json)

    # ------------------------------------------------------------------ data access
    def frame(
        self,
        reference: str,
        *,
        version: int | None = None,
        limit: int | None = None,
        columns: builtins.list[str] | None = None,
    ) -> pl.DataFrame:
        """Materialise a dataset version. Callers must pass a limit for interactive paths."""
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        return self.warehouse.read(row.storage_uri, limit=limit, columns=columns)

    def lazy(self, reference: str, *, version: int | None = None) -> pl.LazyFrame:
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        return self.warehouse.scan(row.storage_uri)

    def preview(
        self, reference: str, *, rows: int = 100, version: int | None = None
    ) -> dict[str, Any]:
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        schema = DatasetSchema.model_validate(row.schema_json)
        frame = self.warehouse.read(row.storage_uri, limit=min(rows, 10_000))
        masked = apply_masking(
            frame,
            schema,
            enabled=self.context.settings.security.mask_restricted_columns,
        )
        return {
            "dataset": dataset.name,
            "version": row.version,
            "rows_total": row.row_count,
            "rows_returned": masked.height,
            "columns": [c.model_dump(mode="json") for c in schema.columns],
            "records": to_records(masked),
            "masked": self.context.settings.security.mask_restricted_columns,
        }

    def query(
        self, sql: str, *, limit: int | None = None, datasets: builtins.list[str] | None = None
    ) -> dict[str, Any]:
        """Guarded SQL over the tenant's datasets, registered as views."""
        require(self.context.principal, Permission.DATASET_READ, resource="dataset")
        policy = SqlPolicy(
            allow_select=True,
            allow_write=self.context.settings.security.sql_write_enabled
            and self.context.principal.has(Permission.SQL_WRITE),
            allow_delete=self.context.settings.security.sql_destructive_enabled
            and self.context.principal.has(Permission.SQL_DESTRUCTIVE),
            allow_ddl=False,
            allow_file_access=False,
            max_rows=limit or self.context.settings.security.sql_max_rows,
            timeout_seconds=self.context.settings.security.sql_statement_timeout_s,
        )
        names = datasets or [d.name for d in self.repo.list(limit=200)]
        with DuckDBEngine(default_policy=policy) as engine:
            registered = []
            for name in names:
                dataset = self.repo.by_name(name)
                if dataset is None:
                    continue
                version = self.repo.latest_version(dataset.id)
                if version is None:
                    continue
                engine.register_parquet(name, version.storage_uri)
                registered.append(name)
            result = engine.query(sql, policy=policy)

        self.context.audit.record(
            self.context.principal,
            "dataset.query",
            "dataset",
            None,
            details={"sql": sql[:500], "rows": result.height, "relations": registered},
        )
        return {
            "columns": result.columns,
            "rows": result.height,
            "records": to_records(result),
            "registered": registered,
        }

    # ------------------------------------------------------------------ profiling & quality
    def profile(
        self, reference: str, *, version: int | None = None, persist: bool = True
    ) -> DatasetProfile:
        require(self.context.principal, Permission.DATASET_READ, resource="dataset")
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        frame = self.warehouse.read(row.storage_uri)
        profile = DataProfiler().profile(frame, dataset=dataset.name, dataset_version_id=row.id)
        if persist:
            self.profiles.create(
                dataset_id=dataset.id,
                dataset_version_id=row.id,
                profile_json=profile.model_dump(mode="json", by_alias=True),
            )
            row.schema_json = profile.schema_.model_dump(mode="json")
            dataset.classification = dataset_classification(profile.schema_).value
            self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "dataset.profile",
            "dataset",
            dataset.id,
            details={"version": row.version, "rows": profile.rows},
        )
        self.context.lineage.record(
            upstream_type="dataset_version",
            upstream_id=row.id,
            downstream_type="analysis",
            downstream_id=f"profile:{row.id}",
            operation="profile",
        )
        return profile

    def latest_profile(self, reference: str) -> DatasetProfile | None:
        dataset = self.get(reference)
        version = self.repo.latest_version(dataset.id)
        if version is None:
            return None
        stored = self.profiles.latest_for_version(version.id)
        if stored is None:
            return None
        return DatasetProfile.model_validate(stored.profile_json)

    def validate(
        self,
        reference: str,
        *,
        version: int | None = None,
        expectations: builtins.list[Expectation] | None = None,
        auto_expectations: bool = False,
        job_id: str | None = None,
        persist: bool = True,
    ) -> QualityReport:
        require(self.context.principal, Permission.DATASET_READ, resource="dataset")
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        frame = self.warehouse.read(row.storage_uri)
        profile = DataProfiler().profile(frame, dataset=dataset.name, dataset_version_id=row.id)

        rules: builtins.list[Expectation] = list(expectations or [])
        if auto_expectations and not rules:
            rules = suggest_expectations(profile)

        report = QualityEngine(self.context.settings.quality).evaluate(
            frame, profile, expectations=rules, dataset_version_id=row.id
        )
        if persist:
            self.quality_reports.create(
                dataset_id=dataset.id,
                dataset_version_id=row.id,
                score=report.score,
                status=report.status,
                report_json=report.model_dump(mode="json"),
                job_id=job_id,
            )
            dataset.quality_score = report.score
            self.context.session.flush()

        if report.status != "pass":
            self.context.alerts.raise_alert(
                rule="data_quality",
                severity=Severity.CRITICAL if report.status == "fail" else Severity.WARNING,
                title=f"Quality {report.status} for '{dataset.name}': {report.score:.1f}/100",
                message="; ".join(f.message for f in report.findings[:3]) or "quality degraded",
                payload={
                    "dataset": dataset.name,
                    "score": report.score,
                    "status": report.status,
                    "version": row.version,
                },
                dedupe_key=f"quality:{dataset.id}:{report.status}",
            )
        self.context.audit.record(
            self.context.principal,
            "dataset.validate",
            "dataset",
            dataset.id,
            details={"score": report.score, "status": report.status, "version": row.version},
        )
        return report

    def quality_history(self, reference: str, *, limit: int = 30) -> builtins.list[dict[str, Any]]:
        dataset = self.get(reference)
        return [
            {
                "score": row.score,
                "status": row.status,
                "at": row.created_at.isoformat(),
                "dataset_version_id": row.dataset_version_id,
            }
            for row in self.quality_reports.history(dataset.id, limit=limit)
        ]

    # ------------------------------------------------------------------ cleaning
    def propose_cleaning(
        self, reference: str, *, version: int | None = None
    ) -> tuple[builtins.list[CleaningProposal], DatasetProfile]:
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        frame = self.warehouse.read(row.storage_uri)
        profile = DataProfiler().profile(frame, dataset=dataset.name, dataset_version_id=row.id)
        report = QualityEngine(self.context.settings.quality).evaluate(frame, profile)
        proposals = CleaningEngine(policy=self.context.policy).propose(frame, profile, report)
        return proposals, profile

    def apply_cleaning(
        self,
        reference: str,
        proposals: builtins.list[CleaningProposal],
        *,
        version: int | None = None,
        approved_ids: set[str] | None = None,
        job_id: str | None = None,
    ) -> tuple[m.DatasetVersion, CleaningResult]:
        """Applies approved proposals and publishes the result as a **new version**."""
        require(self.context.principal, Permission.DATASET_WRITE, resource="dataset")
        dataset = self.get(reference)
        row = self.repo.resolve_version(dataset, version)
        frame = self.warehouse.read(row.storage_uri)

        cleaned, result = CleaningEngine(policy=self.context.policy).apply(
            frame,
            proposals,
            principal=self.context.principal,
            classification=DataClassification(dataset.classification),
            approved_ids=approved_ids,
        )
        if not result.applied:
            raise ValidationFailedError(
                "no cleaning proposal was applied",
                details={"skipped": [p.id for p in result.skipped], "log": result.log},
            )
        new_version = self.write_frame(
            dataset.name,
            cleaned,
            job_id=job_id,
            operation="clean",
            upstream_version_id=row.id,
        )
        self.context.audit.record(
            self.context.principal,
            "dataset.clean",
            "dataset",
            dataset.id,
            details={
                "applied": [p.action for p in result.applied],
                "skipped": [p.action for p in result.skipped],
                "rows_before": result.rows_before,
                "rows_after": result.rows_after,
                "new_version": new_version.version,
            },
        )
        return new_version, result

    # ------------------------------------------------------------------ writing
    def write_frame(
        self,
        name: str,
        frame: pl.DataFrame,
        *,
        job_id: str | None = None,
        operation: str = "write",
        upstream_version_id: str | None = None,
        source_id: str | None = None,
        description: str | None = None,
    ) -> m.DatasetVersion:
        """Publish a frame as a new immutable version of ``name`` (creating the dataset if new)."""
        require(self.context.principal, Permission.DATASET_WRITE, resource="dataset")
        dataset = self.repo.get_or_create(
            name,
            source_id=source_id,
            description=description,
            owner=self.context.principal.email or self.context.principal.user_id,
        )
        previous = self.repo.latest_version(dataset.id)
        version_number = (previous.version + 1) if previous else 1
        write = self.warehouse.write_frame(
            self.context.org_id,
            dataset.name,
            version_number,
            frame,
            metadata={"operation": operation, "job_id": job_id},
            chunk_rows=self.context.settings.ingestion.chunk_rows,
        )
        schema = classify_schema(enrich_schema_semantics(frame, write.schema))
        version = self.repo.add_version(
            dataset,
            version=version_number,
            storage_uri=write.uri,
            row_count=write.rows,
            column_count=write.columns,
            size_bytes=write.size_bytes,
            checksum=write.checksum,
            schema_json=schema.model_dump(mode="json"),
            schema_fingerprint=schema.fingerprint(),
            job_id=job_id,
            created_by=self.context.principal.user_id,
        )
        dataset.classification = dataset_classification(schema).value
        self.context.session.flush()

        self.context.lineage.record(
            upstream_type="dataset",
            upstream_id=dataset.id,
            downstream_type="dataset_version",
            downstream_id=version.id,
            operation="version",
            job_id=job_id,
        )
        if upstream_version_id:
            self.context.lineage.record(
                upstream_type="dataset_version",
                upstream_id=upstream_version_id,
                downstream_type="dataset_version",
                downstream_id=version.id,
                operation=operation,
                job_id=job_id,
            )
        log.info(
            "dataset_version_published",
            dataset=dataset.name,
            version=version_number,
            rows=write.rows,
            operation=operation,
        )
        return version

    def delete_version(self, reference: str, version: int) -> None:
        dataset = self.get(reference)
        decision = self.context.policy.decide(
            self.context.principal,
            "dataset.version.delete",
            classification=DataClassification(dataset.classification),
        )
        decision.enforce()
        row = self.repo.version(dataset.id, version)
        if row is None:
            raise NotFoundError(f"version {version} of '{dataset.name}' not found")
        if decision.needs_human:
            raise ApprovalRequiredError(
                f"deleting version {version} of '{dataset.name}' requires explicit human approval",
                details={"reason": decision.reason, "approval": decision.approval.value},
            )
        self.warehouse.delete_version(self.context.org_id, dataset.name, version)
        self.context.session.delete(row)
        self.context.session.flush()
        self.context.audit.record(
            self.context.principal,
            "dataset.version.delete",
            "dataset",
            dataset.id,
            details={"version": version},
        )

    # ------------------------------------------------------------------ presentation
    @staticmethod
    def to_dict(row: m.Dataset, *, latest: m.DatasetVersion | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "owner": row.owner,
            "classification": row.classification,
            "tags": row.tags or [],
            "current_version": row.current_version,
            "row_count": row.row_count,
            "quality_score": row.quality_score,
            "source_id": row.source_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        if latest is not None:
            payload["latest_version"] = {
                "id": latest.id,
                "version": latest.version,
                "rows": latest.row_count,
                "columns": latest.column_count,
                "size_bytes": latest.size_bytes,
                "checksum": latest.checksum,
                "schema_fingerprint": latest.schema_fingerprint,
                "created_at": latest.created_at.isoformat(),
            }
        return payload


def approval_gate(mode: ApprovalMode) -> bool:
    return mode in {ApprovalMode.AUTO, ApprovalMode.AUTO_WITH_VALIDATION}
