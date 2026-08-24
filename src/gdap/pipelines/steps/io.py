"""I/O steps: reading from sources and datasets, writing versions, producing reports."""

from __future__ import annotations

from typing import Any

from gdap.core.contracts import StepSpec
from gdap.core.enums import ApprovalMode, IngestionMode, ReportFormat
from gdap.core.errors import ValidationFailedError
from gdap.ingestion.engine import IngestRequest
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step
from gdap.reporting.builder import ReportBuilder


@register_step(
    "read.source",
    description="Ingest from a registered source into a dataset version, then load it.",
    read_only=False,
    category="io",
    options={
        "source": "source name or id (required)",
        "object": "file/table/endpoint inside the source",
        "dataset": "target dataset name (defaults to the object name)",
        "mode": "full | incremental | append",
        "incremental_column": "high-water-mark column for incremental loads",
        "dedupe_keys": "list of columns used to deduplicate on append",
        "limit": "maximum rows to read",
    },
)
def read_source(context: StepContext, step: StepSpec) -> StepOutcome:
    request = IngestRequest(
        source=str(context.option(step, "source", required=True)),
        object=context.option(step, "object"),
        dataset=context.option(step, "dataset"),
        mode=IngestionMode(context.option(step, "mode", "full")),
        incremental_column=context.option(step, "incremental_column"),
        dedupe_keys=list(context.option(step, "dedupe_keys", []) or []),
        columns=context.option(step, "columns"),
        limit=context.option(step, "limit"),
        query=context.option(step, "query"),
    )
    result = context.services.sources.ingest(request, job_id=context.job_id)
    frame = context.services.datasets.frame(result.dataset, version=result.version)
    name = step.output or result.dataset
    context.publish(name, frame)
    return StepOutcome(
        frame=frame,
        message=f"ingested {result.rows:,} rows into '{result.dataset}' v{result.version}",
        metrics={
            "dataset": result.dataset,
            "version": result.version,
            "rows": result.rows,
            "bytes": result.bytes_written,
            "checksum": result.checksum,
            "mode": result.mode.value,
        },
    )


@register_step(
    "read.dataset",
    description="Load an existing dataset version into the pipeline.",
    category="io",
    options={
        "dataset": "dataset name or id (required)",
        "version": "specific version number (defaults to latest)",
        "columns": "subset of columns to load",
        "limit": "maximum rows",
    },
)
def read_dataset(context: StepContext, step: StepSpec) -> StepOutcome:
    dataset = str(context.option(step, "dataset", required=True))
    frame = context.services.datasets.frame(
        dataset,
        version=context.option(step, "version"),
        limit=context.option(step, "limit"),
        columns=context.option(step, "columns"),
    )
    context.publish(step.output or dataset, frame)
    return StepOutcome(
        frame=frame,
        message=f"loaded {frame.height:,} rows from '{dataset}'",
        metrics={"dataset": dataset, "rows": frame.height, "columns": frame.width},
    )


@register_step(
    "read.query",
    description="Run guarded SQL across datasets and use the result as the working frame.",
    category="io",
    options={"sql": "SELECT statement (required)", "datasets": "datasets to register as views"},
)
def read_query(context: StepContext, step: StepSpec) -> StepOutcome:
    import polars as pl

    sql = str(context.option(step, "sql", required=True))
    result = context.services.datasets.query(
        sql, datasets=context.option(step, "datasets"), limit=context.option(step, "limit")
    )
    frame = pl.DataFrame(result["records"], infer_schema_length=None, strict=False)
    context.publish(step.output or "query", frame)
    return StepOutcome(
        frame=frame,
        message=f"query returned {frame.height:,} rows",
        metrics={"rows": frame.height, "relations": result["registered"]},
    )


@register_step(
    "write.dataset",
    description="Publish the working frame as a new immutable dataset version.",
    read_only=False,
    approval=ApprovalMode.AUTO,
    category="io",
    options={"dataset": "target dataset name (required)", "description": "catalog description"},
)
def write_dataset(context: StepContext, step: StepSpec) -> StepOutcome:
    name = str(context.option(step, "dataset", required=True))
    frame = context.frame(step.input)
    if context.dry_run:
        return StepOutcome(
            frame=frame,
            message=f"[dry-run] would write {frame.height:,} rows to '{name}'",
            metrics={"dataset": name, "rows": frame.height, "dry_run": True},
        )
    version = context.services.datasets.write_frame(
        name,
        frame,
        job_id=context.job_id,
        operation="pipeline",
        description=context.option(step, "description"),
    )
    context.datasets_written.append(name)
    return StepOutcome(
        frame=frame,
        message=f"wrote {version.row_count:,} rows to '{name}' v{version.version}",
        metrics={
            "dataset": name,
            "version": version.version,
            "rows": version.row_count,
            "checksum": version.checksum,
        },
    )


@register_step(
    "report.generate",
    description="Assemble the analyses produced so far into a report artifact.",
    read_only=False,
    category="reporting",
    options={
        "title": "report title",
        "formats": "list of html|xlsx|csv|json|markdown|pdf",
        "dataset": "dataset the report is about (for lineage and export policy)",
        "include_profile": "attach the data profile section (default false)",
    },
)
def report_generate(context: StepContext, step: StepSpec) -> StepOutcome:
    title = str(context.option(step, "title", f"{context.pipeline} — automated report"))
    formats = [ReportFormat(f) for f in context.option(step, "formats", ["html"]) or ["html"]]
    dataset = context.option(step, "dataset") or (
        context.datasets_written[-1] if context.datasets_written else None
    )

    builder = ReportBuilder(
        title,
        subtitle=f"Pipeline '{context.pipeline}' · run {context.job_id or 'ad-hoc'}",
        locale=context.services.settings.locale.default_locale,
        timezone=context.services.settings.locale.default_timezone,
    )
    for key, value in context.metrics.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            builder.kpi(key.replace("_", " ").title(), value)

    if not context.analyses and not context.insights:
        frame = context.frame(step.input)
        builder.section(
            "Working data",
            body=f"{frame.height:,} rows × {frame.width} columns at the end of the pipeline.",
            table=frame.head(50).to_dicts(),
        )
    for result in context.analyses:
        builder.analysis(result)
    if context.insights:
        builder.insights_section(context.insights, title="Pipeline insights")

    if context.option(step, "include_profile", False) and dataset:
        builder.profile_section(context.services.datasets.profile(dataset))

    builder.metadata(pipeline=context.pipeline, job_id=context.job_id, dataset=dataset)
    spec = builder.build(formats=formats)

    if context.dry_run:
        return StepOutcome(message=f"[dry-run] would render {len(formats)} report artifact(s)")

    reports = context.services.reports.generate(
        spec,
        formats=list(formats),
        dataset=dataset,
        job_id=context.job_id,
    )
    uris = [row.storage_uri for row in reports]
    return StepOutcome(
        message=f"generated {len(reports)} report artifact(s): {', '.join(r.format for r in reports)}",
        artifacts=uris,
        metrics={"reports": len(reports), "formats": [r.format for r in reports]},
    )


@register_step(
    "export.file",
    description="Export the working frame to a file artifact (csv, parquet, json, xlsx).",
    read_only=False,
    approval=ApprovalMode.AUTO_WITH_VALIDATION,
    category="io",
    options={"format": "csv|parquet|json|xlsx", "name": "artifact file name"},
)
def export_file(context: StepContext, step: StepSpec) -> StepOutcome:
    import io as _io

    frame = context.frame(step.input)
    fmt = str(context.option(step, "format", "csv")).lower()
    name = str(context.option(step, "name", f"{context.pipeline}-{context.job_id or 'adhoc'}"))

    buffer = _io.BytesIO()
    if fmt == "csv":
        frame.write_csv(buffer)
    elif fmt == "parquet":
        frame.write_parquet(buffer)
    elif fmt == "json":
        frame.write_json(buffer)
    elif fmt == "xlsx":
        frame.write_excel(buffer)
    else:
        raise ValidationFailedError(
            f"unsupported export format '{fmt}'",
            details={"supported": ["csv", "parquet", "json", "xlsx"]},
        )

    payload = buffer.getvalue()
    if context.dry_run:
        return StepOutcome(frame=frame, message=f"[dry-run] would export {len(payload):,} bytes")

    key = f"{context.services.org_id}/exports/{context.job_id or 'adhoc'}/{name}.{fmt}"
    uri = context.services.platform.artifacts.write_bytes(key, payload)
    context.services.audit.record(
        context.principal,
        "dataset.export",
        "artifact",
        key,
        details={"format": fmt, "rows": frame.height, "bytes": len(payload)},
    )
    return StepOutcome(
        frame=frame,
        message=f"exported {frame.height:,} rows as {fmt}",
        artifacts=[uri],
        metrics={"export_rows": frame.height, "export_bytes": len(payload)},
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]
