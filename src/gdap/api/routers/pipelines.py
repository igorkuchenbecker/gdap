"""Pipeline endpoints: author, version, schedule, run."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import CreatePipelineBody, RunPipelineBody
from gdap.core.enums import TriggerType
from gdap.core.errors import ValidationFailedError
from gdap.core.services.pipeline_service import PipelineService
from gdap.pipelines.spec import parse_spec
from gdap.pipelines.steps import step_catalog

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])


@router.get("", summary="List pipelines")
def list_pipelines(context: ContextDep, page: PaginationDep) -> dict[str, Any]:
    rows = context.pipelines.list(limit=page.limit, offset=page.offset)
    return {"items": [PipelineService.to_dict(row) for row in rows], "count": len(rows)}


@router.get("/steps", summary="Catalogue of available pipeline steps")
def steps() -> dict[str, Any]:
    catalogue = step_catalog()
    return {"items": catalogue, "count": len(catalogue)}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a pipeline")
def create_pipeline(body: CreatePipelineBody, context: ContextDep) -> dict[str, Any]:
    if body.spec is None and not body.yaml:
        raise ValidationFailedError("provide either 'spec' or 'yaml'")
    spec = body.spec or parse_spec(body.yaml or "")
    return PipelineService.to_dict(context.pipelines.create(spec))


@router.get("/{reference}", summary="Get a pipeline")
def get_pipeline(reference: str, context: ContextDep) -> dict[str, Any]:
    row = context.pipelines.get(reference)
    payload = PipelineService.to_dict(row)
    payload["spec"] = row.spec
    return payload


@router.get("/{reference}/yaml", response_class=Response, summary="Get the pipeline as YAML")
def get_pipeline_yaml(reference: str, context: ContextDep) -> Response:
    return Response(content=context.pipelines.as_yaml(reference), media_type="application/yaml")


@router.put("/{reference}", summary="Publish a new pipeline version")
def update_pipeline(
    reference: str, body: CreatePipelineBody, context: ContextDep
) -> dict[str, Any]:
    if body.spec is None and not body.yaml:
        raise ValidationFailedError("provide either 'spec' or 'yaml'")
    spec = body.spec or parse_spec(body.yaml or "")
    return PipelineService.to_dict(context.pipelines.update(reference, spec))


@router.delete("/{reference}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a pipeline")
def delete_pipeline(reference: str, context: ContextDep) -> None:
    context.pipelines.delete(reference)


@router.post("/{reference}/enable", summary="Enable or disable a pipeline")
def set_enabled(reference: str, context: ContextDep, enabled: bool = True) -> dict[str, Any]:
    return PipelineService.to_dict(context.pipelines.set_enabled(reference, enabled))


@router.get("/{reference}/versions", summary="List published pipeline versions")
def versions(reference: str, context: ContextDep) -> dict[str, Any]:
    rows = context.pipelines.versions(reference)
    return {
        "items": [
            {
                "version": row.version,
                "fingerprint": row.fingerprint,
                "created_at": row.created_at.isoformat(),
                "created_by": row.created_by,
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.post(
    "/{reference}/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a pipeline run",
)
def run_pipeline(reference: str, body: RunPipelineBody, context: ContextDep) -> dict[str, Any]:
    job = context.pipelines.run(reference, params=body.params, trigger=TriggerType.API)
    if body.wait:
        result = context.jobs.execute(job)
        return {
            "job_id": job.id,
            "state": result.state.value,
            "result": result.model_dump(mode="json"),
        }
    return {"job_id": job.id, "state": job.state, "queued": True}
