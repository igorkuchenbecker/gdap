"""Dataset endpoints: catalog, preview, profile, validate, clean, query, versions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, status

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import CleaningBody, QueryBody, ValidateBody
from gdap.core.contracts import DatasetProfile, QualityReport
from gdap.core.services.dataset_service import DatasetService

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


@router.get("", summary="List datasets in the catalog")
def list_datasets(context: ContextDep, page: PaginationDep) -> dict[str, Any]:
    rows = context.datasets.list(limit=page.limit, offset=page.offset)
    items = []
    for row in rows:
        latest = context.datasets.repo.latest_version(row.id)
        items.append(DatasetService.to_dict(row, latest=latest))
    return {"items": items, "count": len(items)}


@router.get("/{reference}", summary="Get a dataset with its latest version")
def get_dataset(reference: str, context: ContextDep) -> dict[str, Any]:
    row = context.datasets.get(reference)
    latest = context.datasets.repo.latest_version(row.id)
    payload = DatasetService.to_dict(row, latest=latest)
    payload["quality_history"] = context.datasets.quality_history(reference, limit=10)
    return payload


@router.get("/{reference}/versions", summary="List dataset versions")
def list_versions(reference: str, context: ContextDep) -> dict[str, Any]:
    versions = context.datasets.versions(reference)
    return {
        "items": [
            {
                "id": version.id,
                "version": version.version,
                "rows": version.row_count,
                "columns": version.column_count,
                "size_bytes": version.size_bytes,
                "checksum": version.checksum,
                "schema_fingerprint": version.schema_fingerprint,
                "created_at": version.created_at.isoformat(),
                "job_id": version.job_id,
            }
            for version in versions
        ],
        "count": len(versions),
    }


@router.get("/{reference}/schema", summary="Get the schema of a version")
def get_schema(reference: str, context: ContextDep, version: int | None = None) -> dict[str, Any]:
    return context.datasets.schema(reference, version).model_dump(mode="json")


@router.get("/{reference}/preview", summary="Preview rows (masking applied)")
def preview(
    reference: str,
    context: ContextDep,
    rows: int = Query(default=100, ge=1, le=10_000),
    version: int | None = None,
) -> dict[str, Any]:
    return context.datasets.preview(reference, rows=rows, version=version)


@router.post("/{reference}/profile", summary="Profile a dataset version")
def profile(reference: str, context: ContextDep, version: int | None = None) -> DatasetProfile:
    return context.datasets.profile(reference, version=version)


@router.get("/{reference}/profile", summary="Get the most recent stored profile")
def latest_profile(reference: str, context: ContextDep) -> DatasetProfile | None:
    return context.datasets.latest_profile(reference)


@router.post("/{reference}/validate", summary="Evaluate data quality")
def validate(reference: str, body: ValidateBody, context: ContextDep) -> QualityReport:
    return context.datasets.validate(
        reference,
        version=body.version,
        expectations=body.expectations or None,
        auto_expectations=body.auto_expectations,
    )


@router.post("/{reference}/cleaning", summary="Propose (and optionally apply) cleaning fixes")
def cleaning(reference: str, body: CleaningBody, context: ContextDep) -> dict[str, Any]:
    proposals, _profile = context.datasets.propose_cleaning(reference, version=body.version)
    payload: dict[str, Any] = {
        "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
        "count": len(proposals),
    }
    if body.apply:
        version, result = context.datasets.apply_cleaning(
            reference,
            proposals,
            version=body.version,
            approved_ids=set(body.approve),
        )
        payload["applied"] = result.model_dump(mode="json")
        payload["new_version"] = version.version
    return payload


@router.post("/query", summary="Run guarded SQL across datasets")
def query(body: QueryBody, context: ContextDep) -> dict[str, Any]:
    return context.datasets.query(body.sql, limit=body.limit, datasets=body.datasets)


@router.delete(
    "/{reference}/versions/{version}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one dataset version",
)
def delete_version(reference: str, version: int, context: ContextDep) -> None:
    context.datasets.delete_version(reference, version)
