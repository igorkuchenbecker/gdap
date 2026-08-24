"""Ingestion engine (§6).

Guarantees:

* **bounded memory** — data moves as chunks from the connector straight into a Parquet writer;
* **idempotent versions** — each successful load publishes a new immutable dataset version;
* **checkpoints** — incremental loads resume from the recorded high-water mark;
* **schema evolution** — additive changes are allowed and recorded; breaking ones are refused
  unless explicitly permitted;
* **auditability** — an ``Ingestion`` row exists before any byte is written, so even a crash
  leaves a trace.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from gdap.connectors.base import BaseConnector
from gdap.connectors.registry import ConnectorRegistry, get_registry
from gdap.core.config import Settings
from gdap.core.contracts import (
    DatasetSchema,
    IngestionResult,
    Principal,
    ReadOptions,
    SourceSpec,
)
from gdap.core.enums import DataClassification, IngestionMode
from gdap.core.errors import ConnectorError, IngestionError, SchemaDriftError
from gdap.core.retry import RetryConfig, retry_call
from gdap.governance.audit import AuditTrail
from gdap.governance.classification import classify_schema, dataset_classification
from gdap.governance.lineage import LineageTracker
from gdap.observability.logging import get_logger, log_context
from gdap.observability.metrics import METRICS
from gdap.profiling.semantics import enrich_schema_semantics
from gdap.security.secrets import SecretsResolver
from gdap.storage import models as m
from gdap.storage.repositories import DatasetRepository, IngestionRepository, SourceRepository
from gdap.storage.warehouse import Warehouse

log = get_logger(__name__)


class IngestRequest(BaseModel):
    """What to load, from where, into which dataset."""

    model_config = ConfigDict(extra="forbid")

    source: str
    object: str | None = None
    dataset: str | None = None
    mode: IngestionMode = IngestionMode.FULL
    incremental_column: str | None = None
    since: Any | None = None
    dedupe_keys: list[str] = Field(default_factory=list)
    columns: list[str] | None = None
    limit: int | None = None
    query: str | None = None
    allow_breaking_schema_change: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class IngestionEngine:
    def __init__(
        self,
        session: Session,
        org_id: str,
        settings: Settings,
        warehouse: Warehouse,
        *,
        registry: ConnectorRegistry | None = None,
        secrets: SecretsResolver | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.settings = settings
        self.warehouse = warehouse
        self.registry = registry or get_registry()
        self.secrets = secrets or SecretsResolver(settings)
        self.sources = SourceRepository(session, org_id)
        self.datasets = DatasetRepository(session, org_id)
        self.ingestions = IngestionRepository(session, org_id)
        self.lineage = LineageTracker(session, org_id)
        self.audit = AuditTrail(session, org_id)

    # ------------------------------------------------------------------ public API
    def ingest(
        self, request: IngestRequest, principal: Principal, *, job_id: str | None = None
    ) -> IngestionResult:
        started = datetime.now(UTC)
        source_row = self.sources.require_by_name_or_id(request.source)
        dataset_name = request.dataset or request.object or source_row.name
        dataset = self.datasets.get_or_create(
            dataset_name,
            source_id=source_row.id,
            owner=principal.email or principal.user_id,
        )

        record = self.ingestions.create(
            source_id=source_row.id,
            dataset_id=dataset.id,
            mode=request.mode.value,
            status="running",
            started_at=started,
        )
        self.session.flush()

        with log_context(dataset=dataset_name, source=source_row.name, ingestion_id=record.id):
            try:
                result = self._run(request, source_row, dataset, record, principal, job_id, started)
            except Exception as exc:
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                record.finished_at = datetime.now(UTC)
                self.session.flush()
                METRICS.increment("ingestions_total", status="failed")
                self.audit.record(
                    principal,
                    "source.ingest",
                    "dataset",
                    dataset.id,
                    result="error",
                    details={"error": record.error, "source": source_row.name},
                )
                log.error("ingestion_failed", error=record.error)
                if isinstance(exc, IngestionError | ConnectorError):
                    raise
                raise IngestionError(
                    f"ingestion of '{dataset_name}' failed: {exc}", cause=exc
                ) from exc
            return result

    # ------------------------------------------------------------------ internals
    def _run(
        self,
        request: IngestRequest,
        source_row: m.Source,
        dataset: m.Dataset,
        record: m.Ingestion,
        principal: Principal,
        job_id: str | None,
        started: datetime,
    ) -> IngestionResult:
        spec = _source_spec(source_row)
        secrets = self.secrets.resolve_all(source_row.secret_refs or {})
        connector = self.registry.create(spec, secrets)

        previous_version = self.datasets.latest_version(dataset.id)
        previous_schema = (
            DatasetSchema.model_validate(previous_version.schema_json)
            if previous_version and previous_version.schema_json
            else None
        )
        checkpoint = dict(_last_checkpoint(self.ingestions, dataset.id))
        since = request.since
        if request.mode is IngestionMode.INCREMENTAL and since is None:
            since = checkpoint.get("high_water_mark")

        options = ReadOptions(
            object=request.object,
            limit=request.limit,
            columns=request.columns,
            chunk_rows=self.settings.ingestion.chunk_rows,
            mode=request.mode,
            incremental_column=request.incremental_column,
            since=since,
            query=request.query,
            options=request.options,
        )

        version_number = (previous_version.version + 1) if previous_version else 1
        stats: dict[str, Any] = {"rows": 0, "high_water_mark": since}
        warnings: list[str] = []

        chunks = self._read_with_retry(connector, options, stats, request)
        if request.mode in {IngestionMode.APPEND, IngestionMode.INCREMENTAL} and previous_version:
            chunks = self._prepend_existing(previous_version, chunks, request)

        try:
            write = self.warehouse.write(
                self.org_id,
                dataset.name,
                version_number,
                chunks,
                metadata={
                    "source": source_row.name,
                    "mode": request.mode.value,
                    "ingestion_id": record.id,
                    "job_id": job_id,
                },
            )
        finally:
            connector.close()

        # Semantic types make the catalog immediately useful (masking, charts, the AI layer);
        # inferred from a bounded sample of what was just written, never from the whole file.
        sample = self.warehouse.read(write.uri, limit=self.settings.ingestion.infer_schema_rows)
        schema = classify_schema(enrich_schema_semantics(sample, write.schema))
        diff = previous_schema.diff(schema) if previous_schema else None
        if diff and not diff.is_empty:
            if diff.is_breaking and not (
                request.allow_breaking_schema_change
                or self.settings.ingestion.allow_schema_evolution
            ):
                self.warehouse.delete_version(self.org_id, dataset.name, version_number)
                raise SchemaDriftError(
                    f"breaking schema change in '{dataset.name}'",
                    details=diff.model_dump(),
                )
            warnings.append(f"schema evolved: {diff.model_dump()}")
            log.warning("schema_evolution", dataset=dataset.name, diff=diff.model_dump())

        version = self.datasets.add_version(
            dataset,
            version=version_number,
            storage_uri=write.uri,
            format="parquet",
            row_count=write.rows,
            column_count=write.columns,
            size_bytes=write.size_bytes,
            checksum=write.checksum,
            schema_json=schema.model_dump(mode="json"),
            schema_fingerprint=schema.fingerprint(),
            ingestion_id=record.id,
            job_id=job_id,
            created_by=principal.user_id,
        )
        dataset.classification = dataset_classification(schema).value
        dataset.source_id = source_row.id

        finished = datetime.now(UTC)
        record.status = "success"
        record.records = write.rows
        record.bytes_written = write.size_bytes
        record.finished_at = finished
        record.checkpoint = {
            "high_water_mark": _json_safe(stats.get("high_water_mark")),
            "incremental_column": request.incremental_column,
            "version": version_number,
        }
        source_row.status = "healthy"
        source_row.last_tested_at = finished
        self.session.flush()

        self.lineage.record(
            upstream_type="source",
            upstream_id=source_row.id,
            downstream_type="dataset_version",
            downstream_id=version.id,
            operation=f"ingest:{request.mode.value}",
            job_id=job_id,
        )
        self.lineage.record(
            upstream_type="dataset",
            upstream_id=dataset.id,
            downstream_type="dataset_version",
            downstream_id=version.id,
            operation="version",
            job_id=job_id,
        )
        self.audit.record(
            principal,
            "source.ingest",
            "dataset",
            dataset.id,
            details={
                "source": source_row.name,
                "dataset": dataset.name,
                "version": version_number,
                "rows": write.rows,
                "mode": request.mode.value,
            },
        )
        METRICS.increment("ingestions_total", status="success")
        METRICS.observe("ingestion_rows", write.rows)
        log.info(
            "ingestion_completed",
            dataset=dataset.name,
            version=version_number,
            rows=write.rows,
            bytes=write.size_bytes,
            duration_s=round((finished - started).total_seconds(), 3),
        )

        return IngestionResult(
            dataset=dataset.name,
            dataset_id=dataset.id,
            version=version_number,
            source=source_row.name,
            mode=request.mode,
            rows=write.rows,
            columns=write.columns,
            bytes_written=write.size_bytes,
            checksum=write.checksum,
            storage_uri=write.uri,
            schema=schema,
            schema_diff=diff,
            started_at=started,
            finished_at=finished,
            checkpoint=record.checkpoint,
            warnings=warnings,
        )

    def _read_with_retry(
        self,
        connector: BaseConnector,
        options: ReadOptions,
        stats: dict[str, Any],
        request: IngestRequest,
    ) -> Iterator[pl.DataFrame]:
        """Yield chunks, retrying the *opening* of the stream on transient connector errors."""
        retry_config = RetryConfig(
            attempts=self.settings.ingestion.max_retries,
            backoff_seconds=self.settings.ingestion.retry_backoff_s,
            max_backoff_seconds=self.settings.ingestion.retry_backoff_max_s,
        )
        stream = retry_call(
            lambda: iter(connector.read(options)),
            config=retry_config,
            retry_on=(ConnectorError, OSError, TimeoutError),
            give_up_on=(SchemaDriftError,),
            context=f"connector.read[{connector.name}]",
        )

        first = True
        while True:
            started = time.perf_counter()
            try:
                chunk = next(stream)
            except StopIteration:
                return
            METRICS.observe("ingest_chunk_ms", (time.perf_counter() - started) * 1000)

            if request.dedupe_keys:
                chunk = chunk.unique(subset=request.dedupe_keys, keep="last", maintain_order=True)
            if request.incremental_column and request.incremental_column in chunk.columns:
                current = chunk[request.incremental_column].max()
                previous = stats.get("high_water_mark")
                stats["high_water_mark"] = (
                    current if previous is None else max(current, previous, key=_sort_key)
                )
            stats["rows"] += chunk.height
            if first:
                log.debug("ingest_first_chunk", rows=chunk.height, columns=len(chunk.columns))
                first = False
            yield chunk

    def _prepend_existing(
        self,
        previous_version: m.DatasetVersion,
        chunks: Iterator[pl.DataFrame],
        request: IngestRequest,
    ) -> Iterator[pl.DataFrame]:
        """Append/incremental modes carry the previous version forward, then add new rows."""
        existing = self.warehouse.scan(previous_version.storage_uri)
        if request.dedupe_keys:
            new_frames = list(chunks)
            new_rows = (
                pl.concat(new_frames, how="diagonal_relaxed") if new_frames else pl.DataFrame()
            )
            base = existing.collect()
            if new_rows.is_empty():
                yield base
                return
            keys = [k for k in request.dedupe_keys if k in base.columns and k in new_rows.columns]
            if keys:
                base = base.join(new_rows.select(keys), on=keys, how="anti")
            merged = pl.concat([base, new_rows], how="diagonal_relaxed")
            yield merged
            return

        chunk_rows = self.settings.ingestion.chunk_rows
        total = existing.select(pl.len()).collect().item()
        for offset in range(0, max(total, 1), chunk_rows):
            piece = existing.slice(offset, chunk_rows).collect()
            if piece.is_empty():
                break
            yield piece
        yield from chunks


def _source_spec(row: m.Source) -> SourceSpec:
    return SourceSpec(
        name=row.name,
        type=row.type,  # type: ignore[arg-type]
        connector=row.connector,
        config=row.config or {},
        secret_refs=row.secret_refs or {},
        classification=DataClassification(row.classification),
        description=row.description,
        tags=list(row.tags or []),
    )


def _last_checkpoint(repo: IngestionRepository, dataset_id: str) -> dict[str, Any]:
    previous = repo.list(dataset_id=dataset_id, status="success", limit=1, order_by="finished_at")
    return dict(previous[0].checkpoint or {}) if previous else {}


def _sort_key(value: Any) -> Any:
    return value if not isinstance(value, str) else value


def _json_safe(value: Any) -> Any:
    from gdap.core.frames import json_safe

    return json_safe(value)
