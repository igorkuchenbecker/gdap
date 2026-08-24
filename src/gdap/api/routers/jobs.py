"""Job endpoints: monitor, cancel, approve, retry."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import ApproveBody, RejectBody

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", summary="List jobs")
def list_jobs(
    context: ContextDep,
    page: PaginationDep,
    state: str | None = Query(default=None),
    pipeline: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = context.jobs.list(limit=page.limit, state=state, pipeline=pipeline)
    return {"items": [context.jobs.to_dict(row) for row in rows], "count": len(rows)}


@router.get("/{job_id}", summary="Get a job with its steps")
def get_job(job_id: str, context: ContextDep) -> dict[str, Any]:
    return context.jobs.to_dict(context.jobs.get(job_id), include_steps=True)


@router.post("/{job_id}/execute", summary="Run a queued job inline (no worker required)")
def execute_job(job_id: str, context: ContextDep) -> dict[str, Any]:
    result = context.jobs.execute(context.jobs.get(job_id))
    return result.model_dump(mode="json")


@router.post("/{job_id}/cancel", summary="Cancel a job")
def cancel_job(job_id: str, context: ContextDep, reason: str | None = None) -> dict[str, Any]:
    return context.jobs.to_dict(context.jobs.cancel(job_id, reason=reason))


@router.post("/{job_id}/approve", summary="Approve blocked steps and resume")
def approve_job(job_id: str, body: ApproveBody, context: ContextDep) -> dict[str, Any]:
    return context.jobs.to_dict(
        context.jobs.approve(job_id, steps=body.steps or None, note=body.note)
    )


@router.post("/{job_id}/reject", summary="Reject a pending approval")
def reject_job(job_id: str, body: RejectBody, context: ContextDep) -> dict[str, Any]:
    return context.jobs.to_dict(context.jobs.reject(job_id, reason=body.reason))


@router.post("/{job_id}/retry", summary="Re-queue a failed job")
def retry_job(job_id: str, context: ContextDep) -> dict[str, Any]:
    return context.jobs.to_dict(context.jobs.retry(job_id))
