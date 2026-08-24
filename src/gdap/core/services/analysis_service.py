"""Analysis service: run analyses over dataset versions, persist them, record lineage.

The analytics engine itself is stateless and knows nothing about tenants, storage or audit — this
service is what turns "compute a trend" into a reproducible, traceable platform artifact.
"""

from __future__ import annotations

import builtins
from typing import Any

import polars as pl

from gdap.analytics.engine import AnalyticsEngine
from gdap.core.contracts import AnalysisResult, Insight
from gdap.core.enums import AnalysisKind, Permission, Severity
from gdap.core.errors import NotFoundError
from gdap.core.services.context import ServiceContext
from gdap.observability.logging import get_logger
from gdap.security.rbac import require
from gdap.storage import models as m
from gdap.storage.repositories import AnalysisRepository, DatasetRepository

log = get_logger(__name__)

#: Analyses attempted by :meth:`AnalysisService.auto` — the "just tell me what's going on" path.
AUTO_KINDS: tuple[AnalysisKind, ...] = (
    AnalysisKind.DESCRIBE,
    AnalysisKind.TREND,
    AnalysisKind.COMPARISON,
    AnalysisKind.SEGMENTATION,
    AnalysisKind.DRIVERS,
    AnalysisKind.ANOMALY,
    AnalysisKind.CORRELATION,
)


class AnalysisService:
    def __init__(self, context: ServiceContext) -> None:
        self.context = context
        self.repo = AnalysisRepository(context.session, context.org_id)
        self.datasets = DatasetRepository(context.session, context.org_id)
        self.engine = AnalyticsEngine()

    # ------------------------------------------------------------------ run
    def run(
        self,
        dataset: str,
        kind: AnalysisKind | str,
        *,
        params: dict[str, Any] | None = None,
        version: int | None = None,
        frame: pl.DataFrame | None = None,
        job_id: str | None = None,
        persist: bool = True,
    ) -> AnalysisResult:
        require(self.context.principal, Permission.ANALYSIS_RUN, resource="analysis")
        dataset_row = self.datasets.require_by_name_or_id(dataset)
        version_row = self.datasets.resolve_version(dataset_row, version)

        working = (
            frame
            if frame is not None
            else self.context.platform.warehouse.read(version_row.storage_uri)
        )
        result = self.engine.run(kind, working, dataset=dataset_row.name, params=params)

        if persist:
            row = self.repo.create(
                dataset_id=dataset_row.id,
                dataset_version_id=version_row.id,
                kind=result.kind.value,
                params=result.params,
                result=result.model_dump(mode="json"),
                job_id=job_id,
                created_by=self.context.principal.user_id,
            )
            self.context.lineage.record(
                upstream_type="dataset_version",
                upstream_id=version_row.id,
                downstream_type="analysis",
                downstream_id=row.id,
                operation=f"analyze:{result.kind.value}",
                job_id=job_id,
            )
            self.context.audit.record(
                self.context.principal,
                "analysis.run",
                "analysis",
                row.id,
                details={
                    "dataset": dataset_row.name,
                    "kind": result.kind.value,
                    "version": version_row.version,
                    "insights": len(result.insights),
                },
            )
        return result

    def auto(
        self,
        dataset: str,
        *,
        version: int | None = None,
        kinds: list[AnalysisKind | str] | None = None,
        params: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> builtins.list[AnalysisResult]:
        """Run the analyses that make sense for this dataset, skipping those that cannot apply.

        A dataset without a temporal column simply has no trend — that is a fact about the data,
        not an error, so it is reported and the run continues.
        """
        selected = list(kinds or AUTO_KINDS)
        results: list[AnalysisResult] = []
        skipped: list[dict[str, str]] = []

        dataset_row = self.datasets.require_by_name_or_id(dataset)
        version_row = self.datasets.resolve_version(dataset_row, version)
        frame = self.context.platform.warehouse.read(version_row.storage_uri)

        for kind in selected:
            try:
                results.append(
                    self.run(
                        dataset,
                        kind,
                        params=dict(params or {}),
                        version=version,
                        frame=frame,
                        job_id=job_id,
                    )
                )
            except Exception as exc:
                skipped.append({"kind": str(kind), "reason": f"{type(exc).__name__}: {exc}"})
                log.info("analysis_skipped", kind=str(kind), reason=str(exc)[:200])

        if skipped:
            log.info("analysis_auto_summary", ran=len(results), skipped=len(skipped))
        return results

    # ------------------------------------------------------------------ read
    def get(self, analysis_id: str) -> AnalysisResult:
        require(self.context.principal, Permission.REPORT_READ, resource="analysis")
        row = self.repo.get(analysis_id)
        if row is None:
            raise NotFoundError(f"analysis '{analysis_id}' not found")
        return AnalysisResult.model_validate(row.result)

    def list(
        self, *, dataset: str | None = None, kind: str | None = None, limit: int = 50
    ) -> builtins.list[m.Analysis]:
        require(self.context.principal, Permission.REPORT_READ, resource="analysis")
        dataset_id = self.datasets.require_by_name_or_id(dataset).id if dataset else None
        return self.repo.list(limit=limit, dataset_id=dataset_id, kind=kind)

    def insights(
        self, dataset: str, *, limit: int = 20, min_severity: Severity | None = None
    ) -> builtins.list[Insight]:
        """Recent insights for a dataset, most severe first — what the dashboard shows."""
        collected: list[Insight] = []
        for row in self.list(dataset=dataset, limit=limit):
            result = AnalysisResult.model_validate(row.result)
            collected.extend(result.insights)
        order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
        if min_severity is not None:
            allowed = {s for s in Severity if order[s] <= order[min_severity]}
            collected = [insight for insight in collected if insight.severity in allowed]
        collected.sort(key=lambda insight: (order[insight.severity], -insight.confidence))
        return collected[:limit]

    @staticmethod
    def to_dict(row: m.Analysis) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "dataset_id": row.dataset_id,
            "dataset_version_id": row.dataset_version_id,
            "params": row.params or {},
            "summary": (row.result or {}).get("summary"),
            "insights": len((row.result or {}).get("insights", [])),
            "job_id": row.job_id,
            "created_at": row.created_at.isoformat(),
            "created_by": row.created_by,
        }
