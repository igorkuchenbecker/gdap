"""Source endpoints: register, probe, discover, ingest."""

from __future__ import annotations

import contextlib
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile, status

from gdap.api.deps import ContextDep, PaginationDep
from gdap.api.schemas import CreateSourceRequest, IngestBody
from gdap.core.contracts import ConnectionTestResult, PipelineSpec, SourceSpec, StepSpec
from gdap.core.enums import DataFormat, IngestionMode, Permission, SourceType, TriggerType
from gdap.core.errors import PayloadTooLargeError, UnsupportedOperationError, ValidationFailedError
from gdap.core.services.source_service import SourceService
from gdap.ingestion import IngestRequest
from gdap.security.rbac import require

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])

# The upload endpoint deliberately accepts a narrower set than the file connector as a whole
# (which also reads XML and Arrow): these are the formats considered safe and ready for an
# unattended, browser-driven "drop a file in" flow.
_UPLOAD_EXTENSIONS: dict[str, DataFormat] = {
    ".csv": DataFormat.CSV,
    ".tsv": DataFormat.TSV,
    ".json": DataFormat.JSON,
    ".ndjson": DataFormat.NDJSON,
    ".jsonl": DataFormat.NDJSON,
    ".parquet": DataFormat.PARQUET,
    ".pq": DataFormat.PARQUET,
    ".xlsx": DataFormat.EXCEL,
    ".xls": DataFormat.EXCEL,
}

_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_SLUG = re.compile(r"[^a-z0-9]+")


def _discard_upload(target: Path) -> None:
    """Best-effort cleanup of a rejected/failed upload and its now-empty staging directory."""
    target.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        target.parent.rmdir()


def _slugify(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")[:80]


def _split_upload_filename(filename: str) -> tuple[str, str]:
    """Derive a safe (slug, extension) pair from a client-supplied filename.

    Only the basename is ever used — any directory components the client sends (``../../etc``,
    absolute paths, backslash-style traversal) are discarded, so the result can never point
    outside the staging directory it is later joined with.
    """
    basename = Path(filename).name
    suffix = Path(basename).suffix.lower()
    stem = basename[: len(basename) - len(suffix)] if suffix else basename
    return _slugify(stem) or "upload", suffix


@router.get("", summary="List registered sources")
def list_sources(context: ContextDep, page: PaginationDep) -> dict[str, Any]:
    rows = context.sources.list(limit=page.limit, offset=page.offset)
    return {"items": [SourceService.to_dict(row) for row in rows], "count": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register a source")
def create_source(body: CreateSourceRequest, context: ContextDep) -> dict[str, Any]:
    return SourceService.to_dict(context.sources.register(body.to_spec()))


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a local file and ingest it in one step",
)
def upload_source(
    context: ContextDep,
    file: Annotated[UploadFile, File(description="CSV, TSV, JSON/NDJSON, Parquet or Excel")],
    dataset: Annotated[str | None, Form()] = None,
    source: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Stream an uploaded file to staging, register it as a file source, and ingest it.

    This is the no-JSON, no-manual-file-prep on-ramp for the web UI: pick a file, get a
    queryable dataset back. Everything else (tenant scoping, audit, the ingestion transaction)
    goes through the same :class:`SourceService` the JSON API uses.

    Declared ``def``, not ``async def``, like every other handler in this API. The work below is
    blocking from end to end -- a synchronous SQLAlchemy session, then a full ingestion that reads
    the file in chunks and writes Parquet. On an ``async def`` handler all of that runs *on the
    event loop*, so one person importing a large file stalls every other request in the process,
    health checks included. As a plain ``def`` FastAPI runs it in the threadpool, where blocking
    work belongs.
    """
    # Checked again inside SourceService.register/ingest (defense in depth). Checking here first
    # means a principal without these permissions never gets a byte written to *staging* — the
    # permanent location. It does not stop the body being read: by the time any handler runs,
    # FastAPI has resolved `file`, and resolving an UploadFile means Starlette already parsed and
    # spooled the whole multipart body. What keeps an oversized body from being buffered at all
    # is BodySizeLimitMiddleware, which runs ahead of the parser.
    require(
        context.principal,
        Permission.SOURCE_WRITE,
        Permission.DATASET_WRITE,
        resource="source upload",
    )

    if not file.filename:
        raise ValidationFailedError("the uploaded file must have a name")

    slug, suffix = _split_upload_filename(file.filename)
    fmt = _UPLOAD_EXTENSIONS.get(suffix)
    if fmt is None:
        raise UnsupportedOperationError(
            f"unsupported file extension '{suffix or '(none)'}'",
            details={"filename": file.filename, "supported": sorted(_UPLOAD_EXTENSIONS)},
        )

    upload_id = uuid.uuid4().hex
    # An explicit `source` names a durable connection the caller intends to reuse; an omitted one
    # gets a per-upload suffix so re-importing the same file (a new drop of "monthly_revenue.csv")
    # doesn't 409 against the source created by the previous import. The dataset name stays stable
    # either way, so repeated uploads land as new versions of the same dataset.
    source_name = source or f"{slug}-{upload_id[:8]}"
    dataset_name = dataset or slug
    limit_bytes = context.settings.api.max_upload_mb * 1024 * 1024

    staging = context.platform.staging
    key = f"{context.org_id}/uploads/{upload_id}/{slug}{suffix}"
    target = staging.local_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    try:
        # ``file.file`` is the underlying spooled temporary file. Reading it directly keeps this
        # handler synchronous; ``UploadFile.read`` is a coroutine and would force the whole
        # endpoint back onto the event loop for no benefit.
        with target.open("wb") as buffer:
            while chunk := file.file.read(_UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > limit_bytes:
                    raise PayloadTooLargeError(
                        f"upload exceeds the {context.settings.api.max_upload_mb} MB limit",
                        details={"limit_mb": context.settings.api.max_upload_mb},
                    )
                buffer.write(chunk)
        if written == 0:
            raise ValidationFailedError("the uploaded file is empty")
    except Exception:
        _discard_upload(target)
        raise
    finally:
        file.file.close()

    try:
        row = context.sources.register(
            SourceSpec(
                name=source_name,
                type=SourceType.FILE,
                connector="file",
                config={"path": str(target), "format": fmt.value},
                description=f"Uploaded file '{file.filename}'",
            )
        )
        result = context.sources.ingest(
            IngestRequest(source=row.name, dataset=dataset_name, mode=IngestionMode.FULL)
        )
    except Exception:
        # The request is one transaction (gdap.api.deps.service_context): a failure here rolls
        # the source registration back too, so this staging file — which lives in a upload-unique
        # directory nothing else can reach — is always safe to remove.
        _discard_upload(target)
        raise

    return {
        "status": "completed",
        "source": SourceService.to_dict(row),
        "result": result.model_dump(mode="json", by_alias=True),
    }


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
