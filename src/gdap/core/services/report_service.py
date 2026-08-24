"""Report service: assemble, render, store and serve report artifacts (§23).

A report is an *artifact with provenance*: the rendered bytes live in the artifact store, the
metadata row records who generated it from which dataset version, and a lineage edge ties it back
to the analyses it was built from. Exporting data classified above the tenant's threshold is a
policy decision, enforced here (§44/§46).
"""

from __future__ import annotations

import re
from typing import Any

from gdap.core.contracts import AnalysisResult, ReportSpec
from gdap.core.enums import AnalysisKind, DataClassification, Permission, ReportFormat
from gdap.core.errors import NotFoundError
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.reporting.builder import ReportBuilder
from gdap.reporting.renderers import render
from gdap.security.rbac import require
from gdap.storage import models as m
from gdap.storage.repositories import DatasetRepository, ReportRepository

log = get_logger(__name__)

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")[:80] or "report"


class ReportService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = ReportRepository(context.session, context.org_id)
        self.datasets = DatasetRepository(context.session, context.org_id)
        self.storage = context.platform.artifacts

    # ------------------------------------------------------------------ generate
    def generate(
        self,
        spec: ReportSpec,
        *,
        formats: list[ReportFormat | str] | None = None,
        dataset: str | None = None,
        job_id: str | None = None,
        analysis_ids: list[str] | None = None,
    ) -> list[m.Report]:
        """Render a report spec into one artifact per requested format."""
        require(self.context.principal, Permission.REPORT_WRITE, resource="report")

        dataset_row = self.datasets.by_name(dataset) if dataset else None
        classification = (
            DataClassification(dataset_row.classification)
            if dataset_row
            else DataClassification.INTERNAL
        )
        self.context.policy.may_export(self.context.principal, classification).enforce()

        requested = [ReportFormat(f) for f in (formats or spec.formats or [ReportFormat.HTML])]
        produced: list[m.Report] = []

        for report_format in requested:
            payload, extension, media_type = render(spec, report_format)
            row = self.repo.create(
                name=spec.title,
                format=report_format.value,
                storage_uri="",
                size_bytes=len(payload),
                job_id=job_id,
                dataset_id=dataset_row.id if dataset_row else None,
                summary={
                    "subtitle": spec.subtitle,
                    "executive_summary": spec.executive_summary,
                    "kpis": spec.kpis,
                    "sections": [section.title for section in spec.sections],
                    "media_type": media_type,
                    "dataset": dataset_row.name if dataset_row else None,
                },
                created_by=self.context.principal.user_id,
            )
            key = f"{self.context.org_id}/reports/{row.id}/{slugify(spec.title)}.{extension}"
            row.storage_uri = self.storage.write_bytes(key, payload)
            self.context.session.flush()

            for analysis_id in analysis_ids or []:
                self.context.lineage.record(
                    upstream_type="analysis",
                    upstream_id=analysis_id,
                    downstream_type="report",
                    downstream_id=row.id,
                    operation="report",
                    job_id=job_id,
                )
            if dataset_row:
                version = self.datasets.latest_version(dataset_row.id)
                if version:
                    self.context.lineage.record(
                        upstream_type="dataset_version",
                        upstream_id=version.id,
                        downstream_type="report",
                        downstream_id=row.id,
                        operation=f"report:{report_format.value}",
                        job_id=job_id,
                    )
            self.context.audit.record(
                self.context.principal,
                "report.generate",
                "report",
                row.id,
                details={
                    "format": report_format.value,
                    "bytes": len(payload),
                    "dataset": dataset_row.name if dataset_row else None,
                    "classification": classification.value,
                },
            )
            METRICS.increment("reports_total", format=report_format.value)
            produced.append(row)

        log.info(
            "report_generated",
            title=spec.title,
            formats=[f.value for f in requested],
            artifacts=len(produced),
        )
        return produced

    def dataset_report(
        self,
        dataset: str,
        *,
        title: str | None = None,
        kinds: list[AnalysisKind | str] | None = None,
        params: dict[str, Any] | None = None,
        formats: list[ReportFormat | str] | None = None,
        version: int | None = None,
        include_profile: bool = True,
        include_quality: bool = True,
        job_id: str | None = None,
    ) -> tuple[list[m.Report], ReportSpec]:
        """The end-to-end path: profile → validate → analyse → assemble → render → store."""
        dataset_row = self.datasets.require_by_name_or_id(dataset)
        builder = ReportBuilder(
            title or f"{dataset_row.name} — automated analysis",
            subtitle=f"GDAP report · dataset '{dataset_row.name}'",
            locale=self.context.settings.locale.default_locale,
            timezone=self.context.settings.locale.default_timezone,
        )

        version_row = self.datasets.resolve_version(dataset_row, version)
        builder.kpi("Rows", version_row.row_count)
        builder.kpi("Columns", version_row.column_count)
        builder.kpi("Version", version_row.version)

        if include_quality:
            quality = self.context.datasets.validate(
                dataset_row.name, version=version, auto_expectations=True, job_id=job_id
            )
            builder.quality_section(quality)

        analyses: list[AnalysisResult] = self.context.analyses.auto(
            dataset_row.name, version=version, kinds=kinds, params=params, job_id=job_id
        )
        for result in analyses:
            builder.analysis(result)

        if include_profile:
            profile = self.context.datasets.profile(dataset_row.name, version=version)
            builder.profile_section(profile)

        builder.metadata(
            dataset=dataset_row.name,
            dataset_version=version_row.version,
            dataset_version_id=version_row.id,
            checksum=version_row.checksum,
            job_id=job_id,
        )
        builder.methodology(
            f"source dataset version v{version_row.version} (checksum {version_row.checksum[:12]}), "
            f"analysed with GDAP {_platform_version()}"
        )

        spec = builder.build(formats=[ReportFormat(f) for f in (formats or [ReportFormat.HTML])])
        analysis_rows = self.context.analyses.list(dataset=dataset_row.name, limit=len(analyses))
        reports = self.generate(
            spec,
            formats=formats,
            dataset=dataset_row.name,
            job_id=job_id,
            analysis_ids=[row.id for row in analysis_rows],
        )
        return reports, spec

    # ------------------------------------------------------------------ read
    def get(self, report_id: str) -> m.Report:
        require(self.context.principal, Permission.REPORT_READ, resource="report")
        row = self.repo.get(report_id)
        if row is None:
            raise NotFoundError(f"report '{report_id}' not found")
        return row

    def list(self, *, limit: int = 50, job_id: str | None = None) -> list[m.Report]:
        require(self.context.principal, Permission.REPORT_READ, resource="report")
        return self.repo.list(limit=limit, job_id=job_id)

    def download(self, report_id: str) -> tuple[bytes, str, str]:
        """Return ``(payload, media_type, filename)`` for an artifact, audited as an export."""
        row = self.get(report_id)
        key = row.storage_uri.replace(f"file://{self.storage.root}/", "")
        payload = self.storage.read_bytes(key)
        media_type = (row.summary or {}).get("media_type", "application/octet-stream")
        filename = f"{slugify(row.name)}.{row.format}"
        self.context.audit.record(
            self.context.principal,
            "report.download",
            "report",
            row.id,
            details={"format": row.format, "bytes": len(payload)},
        )
        return payload, media_type, filename

    @staticmethod
    def to_dict(row: m.Report) -> dict[str, Any]:
        summary = row.summary or {}
        return {
            "id": row.id,
            "name": row.name,
            "format": row.format,
            "size_bytes": row.size_bytes,
            "dataset_id": row.dataset_id,
            "dataset": summary.get("dataset"),
            "job_id": row.job_id,
            "executive_summary": summary.get("executive_summary"),
            "kpis": summary.get("kpis", []),
            "sections": summary.get("sections", []),
            "created_at": row.created_at.isoformat(),
            "created_by": row.created_by,
            "download_url": f"/api/v1/reports/{row.id}/download",
        }


def _platform_version() -> str:
    from gdap import __version__

    return __version__
