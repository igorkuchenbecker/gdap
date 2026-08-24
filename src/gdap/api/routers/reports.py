"""Report endpoints: generate, list, download."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import ReportBody
from gdap.core.services.report_service import ReportService

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.get("", summary="List report artifacts")
def list_reports(context: ContextDep, page: PaginationDep) -> dict[str, Any]:
    rows = context.reports.list(limit=page.limit)
    return {"items": [ReportService.to_dict(row) for row in rows], "count": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Generate a dataset report")
def create_report(body: ReportBody, context: ContextDep) -> dict[str, Any]:
    reports, spec = context.reports.dataset_report(
        body.dataset,
        title=body.title,
        kinds=list(body.kinds) if body.kinds else None,
        params=body.params,
        formats=list(body.formats),
        include_profile=body.include_profile,
        include_quality=body.include_quality,
    )
    return {
        "items": [ReportService.to_dict(row) for row in reports],
        "executive_summary": spec.executive_summary,
        "kpis": spec.kpis,
    }


@router.get("/{report_id}", summary="Get report metadata")
def get_report(report_id: str, context: ContextDep) -> dict[str, Any]:
    return ReportService.to_dict(context.reports.get(report_id))


@router.get("/{report_id}/download", response_class=Response, summary="Download the artifact")
def download_report(report_id: str, context: ContextDep) -> Response:
    payload, media_type, filename = context.reports.download(report_id)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{report_id}/view", response_class=Response, summary="View an HTML report inline")
def view_report(report_id: str, context: ContextDep) -> Response:
    payload, media_type, _filename = context.reports.download(report_id)
    return Response(content=payload, media_type=media_type)
