"""Analytical storage: immutable, versioned dataset files.

Every ingestion or pipeline write produces a **new version directory**:

``{org_id}/{dataset}/v{n}/data.parquet`` + ``_manifest.json``

Immutability is what makes reproducibility (§42), rollback and lineage possible: a job result can
always be traced back to the exact bytes it read. Writes stream in chunks, so dataset size is
bounded by disk, not RAM (§19).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from gdap.core.contracts import DatasetSchema
from gdap.core.errors import StorageError
from gdap.core.frames import schema_from_frame
from gdap.observability.logging import get_logger
from gdap.storage.backends import LocalFileStorage

log = get_logger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip())
    if not cleaned:
        raise StorageError(f"invalid dataset name: {name!r}")
    return cleaned[:120]


@dataclass(slots=True)
class WriteResult:
    key: str
    uri: str
    rows: int
    columns: int
    size_bytes: int
    checksum: str
    schema: DatasetSchema
    manifest: dict[str, Any] = field(default_factory=dict)


class Warehouse:
    """Versioned columnar storage over a :class:`StorageBackend`."""

    def __init__(self, storage: LocalFileStorage) -> None:
        self.storage = storage

    # ------------------------------------------------------------------ paths
    def version_key(self, org_id: str, dataset: str, version: int) -> str:
        return f"{safe_name(org_id)}/{safe_name(dataset)}/v{version}"

    def data_key(self, org_id: str, dataset: str, version: int) -> str:
        return f"{self.version_key(org_id, dataset, version)}/data.parquet"

    def next_version(self, org_id: str, dataset: str, current: int) -> int:
        return current + 1

    # ------------------------------------------------------------------ write
    def write(
        self,
        org_id: str,
        dataset: str,
        version: int,
        chunks: Iterable[pl.DataFrame],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WriteResult:
        """Stream chunks into one Parquet file, then publish a manifest atomically."""
        key = self.data_key(org_id, dataset, version)
        path = self.storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".parquet.tmp")

        writer: pq.ParquetWriter | None = None
        rows = 0
        schema: DatasetSchema | None = None
        arrow_schema: pa.Schema | None = None
        try:
            for chunk in chunks:
                if chunk.is_empty() and writer is not None:
                    continue
                table = chunk.to_arrow()
                if writer is None:
                    arrow_schema = table.schema
                    writer = pq.ParquetWriter(tmp_path, arrow_schema, compression="zstd")
                    schema = schema_from_frame(chunk, version=version)
                elif arrow_schema is not None and table.schema != arrow_schema:
                    dropped = [c for c in table.column_names if c not in arrow_schema.names]
                    if dropped:
                        log.warning(
                            "chunk_columns_dropped",
                            dataset=dataset,
                            version=version,
                            columns=dropped,
                            reason="schema is fixed by the first chunk of a write",
                        )
                    table = _align(table, arrow_schema)
                writer.write_table(table)
                rows += chunk.height
            if writer is None:  # nothing was produced: still create an empty, typed file
                empty = pl.DataFrame()
                schema = schema_from_frame(empty, version=version)
                pq.write_table(empty.to_arrow(), tmp_path, compression="zstd")
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise StorageError(f"failed writing dataset '{dataset}' v{version}", cause=exc) from exc
        finally:
            if writer is not None:
                writer.close()

        tmp_path.replace(path)
        size = path.stat().st_size
        checksum = _sha256_file(path)
        assert schema is not None
        manifest = {
            "dataset": dataset,
            "org_id": org_id,
            "version": version,
            "rows": rows,
            "columns": len(schema.columns),
            "size_bytes": size,
            "checksum": checksum,
            "schema": schema.model_dump(mode="json"),
            "schema_fingerprint": schema.fingerprint(),
            "written_at": datetime.now(UTC).isoformat(),
            "format": "parquet",
            "compression": "zstd",
            **(metadata or {}),
        }
        self.storage.write_bytes(
            f"{self.version_key(org_id, dataset, version)}/_manifest.json",
            json.dumps(manifest, indent=2, default=str).encode(),
        )
        log.info(
            "dataset_version_written",
            dataset=dataset,
            version=version,
            rows=rows,
            size_bytes=size,
        )
        return WriteResult(
            key=key,
            uri=self.storage.uri(key),
            rows=rows,
            columns=len(schema.columns),
            size_bytes=size,
            checksum=checksum,
            schema=schema,
            manifest=manifest,
        )

    def write_frame(
        self,
        org_id: str,
        dataset: str,
        version: int,
        frame: pl.DataFrame,
        *,
        metadata: dict[str, Any] | None = None,
        chunk_rows: int = 250_000,
    ) -> WriteResult:
        return self.write(
            org_id, dataset, version, _slice_frame(frame, chunk_rows), metadata=metadata
        )

    # ------------------------------------------------------------------ read
    def path_for_uri(self, uri: str) -> Path:
        if uri.startswith("file://"):
            return Path(uri[len("file://") :])
        raise StorageError(f"unsupported storage uri: {uri}")

    def scan(self, uri: str) -> pl.LazyFrame:
        """Lazy scan — predicate/projection pushdown keeps memory bounded."""
        path = self.path_for_uri(uri)
        if not path.exists():
            raise StorageError(f"dataset file missing: {path}")
        return pl.scan_parquet(path)

    def read(
        self, uri: str, *, limit: int | None = None, columns: list[str] | None = None
    ) -> pl.DataFrame:
        lazy = self.scan(uri)
        if columns:
            lazy = lazy.select(columns)
        if limit is not None:
            lazy = lazy.limit(limit)
        return lazy.collect()

    def manifest(self, org_id: str, dataset: str, version: int) -> dict[str, Any]:
        key = f"{self.version_key(org_id, dataset, version)}/_manifest.json"
        return json.loads(self.storage.read_bytes(key))

    def delete_version(self, org_id: str, dataset: str, version: int) -> None:
        self.storage.delete(self.version_key(org_id, dataset, version))


def _slice_frame(frame: pl.DataFrame, chunk_rows: int) -> Iterator[pl.DataFrame]:
    if frame.height <= chunk_rows:
        yield frame
        return
    for offset in range(0, frame.height, chunk_rows):
        yield frame.slice(offset, chunk_rows)


def _align(table: pa.Table, target: pa.Schema) -> pa.Table:
    """Reconcile an evolved chunk with the file schema (additive evolution only)."""
    arrays = []
    for field_ in target:
        if field_.name in table.column_names:
            column = table.column(field_.name)
            if column.type != field_.type:
                column = column.cast(field_.type)
            arrays.append(column)
        else:
            arrays.append(pa.nulls(table.num_rows, type=field_.type))
    return pa.Table.from_arrays(
        [pa.chunked_array(a) if not isinstance(a, pa.ChunkedArray) else a for a in arrays],
        schema=target,
    )


def _sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()
