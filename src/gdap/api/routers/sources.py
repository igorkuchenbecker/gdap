"""Source endpoints: register, probe, discover, ingest."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import CreateSourceRequest, IngestBody
from gdap.core.contracts import ConnectionTestResult, PipelineSpec, StepSpec
from gdap.core.enums import TriggerType
from gdap.core.services.source_service import SourceService
from gdap.ingestion import IngestRequest

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("", summary="List registered sources")
def list_sources(context: ContextDep, page: PaginationDep) -> dict[str, Any]:
    rows = context.sources.list(limit=page.limit, offset=page.offset)
    return {"items": [SourceService.to_dict(row) for row in rows], "count": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register a source")
def create_source(body: CreateSourceRequest, context: ContextDep) -> dict[str, Any]:
    return SourceService.to_dict(context.sources.register(body.to_spec()))


@router.get("/{reference}", summary="Get one source")
def get_source(reference: str, context: ContextDep) -> dict[str, Any]:
    return SourceService.to_dict(context.sources.get(reference))


@router.delete("/{reference}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a source")
def delete_source(reference: str, context: ContextDep) -> None:
    context.sources.delete(reference)


@router.post("/{reference}/test", summary="Probe connectivity and permissions")
def test_source(reference: str, context: ContextDep) -> ConnectionTestResult:
    return context.sources.test(reference)


@router.get("/{reference}/discover", summary="List objects available in the source")
def discover_source(reference: str, context: ContextDep) -> dict[str, Any]:
    objects = context.sources.discover(reference)
    return {
        "items": [obj.model_dump(mode="json", by_alias=True) for obj in objects],
        "count": len(objects),
    }


@router.post("/{reference}/ingest", summary="Ingest data from the source into a dataset")
def ingest(reference: str, body: IngestBody, context: ContextDep) -> dict[str, Any]:
    request = IngestRequest(
        source=reference,
        object=body.object,
        dataset=body.dataset,
        mode=body.mode,
        incremental_column=body.incremental_column,
        dedupe_keys=body.dedupe_keys,
        columns=body.columns,
        limit=body.limit,
        query=body.query,
    )
    if not body.async_:
        result = context.sources.ingest(request)
        return {"status": "completed", "result": result.model_dump(mode="json", by_alias=True)}

    # Asynchronous ingestion is just a one-step pipeline — same engine, same observability.
    spec = PipelineSpec(
        name=f"ingest_{body.dataset or body.object or reference}",
        description=f"Ad-hoc ingestion from '{reference}'",
        steps=[
            StepSpec.of(
                "read.source",
                id="ingest",
                options={
                    "source": reference,
                    "object": body.object,
                    "dataset": body.dataset,
                    "mode": body.mode.value,
                    "incremental_column": body.incremental_column,
                    "dedupe_keys": body.dedupe_keys,
                },
            )
        ],
    )
    job = context.jobs.create(pipeline=None, spec=spec, trigger=TriggerType.API)
    return {"status": "queued", "job_id": job.id, "state": job.state}
