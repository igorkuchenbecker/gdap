"""File connector: CSV/TSV, JSON/NDJSON, Parquet, XML and Excel, local or mounted.

Reads are chunked wherever the format allows it (CSV and Parquet stream natively; JSON/XML are
row-sliced after parse). A single source can point at one file, a directory or a glob — that is
what makes "drop a file in this folder every night" work without a new connector.
"""

from __future__ import annotations

import gzip
import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import polars as pl

from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.contracts import DatasetSchema, DiscoveredObject, ReadOptions, SourceSpec
from gdap.core.enums import DataFormat, SourceType
from gdap.core.errors import ConnectorError, UnsupportedOperationError
from gdap.core.frames import schema_from_frame
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_EXTENSION_FORMATS: dict[str, DataFormat] = {
    ".csv": DataFormat.CSV,
    ".tsv": DataFormat.TSV,
    ".txt": DataFormat.CSV,
    ".json": DataFormat.JSON,
    ".ndjson": DataFormat.NDJSON,
    ".jsonl": DataFormat.NDJSON,
    ".parquet": DataFormat.PARQUET,
    ".pq": DataFormat.PARQUET,
    ".xml": DataFormat.XML,
    ".xlsx": DataFormat.EXCEL,
    ".xls": DataFormat.EXCEL,
    ".arrow": DataFormat.ARROW,
    ".ipc": DataFormat.ARROW,
}
_COMPRESSED = {".gz", ".zip", ".zst", ".bz2"}


def detect_format(path: Path) -> DataFormat:
    suffixes = [s.lower() for s in path.suffixes]
    for suffix in reversed(suffixes):
        if suffix in _EXTENSION_FORMATS:
            return _EXTENSION_FORMATS[suffix]
    raise ConnectorError(
        f"cannot detect format for '{path.name}'",
        details={
            "hint": "set config['format'] explicitly",
            "supported": sorted(_EXTENSION_FORMATS),
        },
    )


class FileConnector(BaseConnector):
    """Config: ``path`` (file | directory | glob), optional ``format``, parser options."""

    key = "file"
    source_type = SourceType.FILE

    def __init__(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> None:
        super().__init__(spec, secrets)
        self.root = Path(str(self.require("path"))).expanduser()
        self.pattern: str = self.config.get("pattern", "*")
        self.explicit_format: str | None = self.config.get("format")

    # ------------------------------------------------------------------ discovery
    def _files(self) -> list[Path]:
        if any(ch in str(self.root) for ch in "*?["):
            base = Path(self.root.anchor or ".")
            relative = str(self.root).replace(str(base), "", 1).lstrip("/")
            return sorted(p for p in base.glob(relative) if p.is_file())
        if self.root.is_file():
            return [self.root]
        if self.root.is_dir():
            return sorted(p for p in self.root.glob(self.pattern) if p.is_file())
        raise ConnectorError(
            f"path does not exist: {self.root}",
            details={"source": self.name},
        )

    def discover(self) -> list[DiscoveredObject]:
        objects: list[DiscoveredObject] = []
        for path in self._files():
            try:
                fmt = (
                    DataFormat(self.explicit_format)
                    if self.explicit_format
                    else detect_format(path)
                )
            except (ConnectorError, ValueError):
                continue
            rows: int | None = None
            if fmt is DataFormat.PARQUET:
                try:
                    import pyarrow.parquet as pq

                    rows = pq.ParquetFile(path).metadata.num_rows
                except Exception:  # pragma: no cover - unreadable footer
                    rows = None
            objects.append(
                DiscoveredObject(
                    name=path.name,
                    kind="file",
                    location=str(path),
                    format=fmt,
                    estimated_rows=rows,
                    size_bytes=path.stat().st_size,
                )
            )
        return objects

    def infer_schema(self, options: ReadOptions) -> DatasetSchema:
        path = self._resolve_one(options)
        fmt = self._format_for(path)
        if fmt is DataFormat.PARQUET:
            return schema_from_frame(pl.scan_parquet(path))
        if fmt in (DataFormat.CSV, DataFormat.TSV) and path.suffix.lower() not in _COMPRESSED:
            lazy = pl.scan_csv(
                path,
                separator=self._separator(fmt),
                has_header=self.config.get("has_header", True),
                infer_schema_length=self.config.get("infer_schema_rows", 10_000),
                try_parse_dates=self.config.get("parse_dates", True),
                encoding="utf8-lossy",
            )
            return schema_from_frame(lazy)
        return super().infer_schema(options)

    # ------------------------------------------------------------------ reading
    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]:
        paths = [self._resolve_one(options)] if options.object else self._files()
        if not paths:
            raise ConnectorError(f"no files matched for source '{self.name}'")

        emitted = 0
        for path in paths:
            for chunk in self._read_file(path, options):
                chunk = self._post_process(chunk, options)
                if chunk.is_empty():
                    continue
                if options.limit is not None:
                    remaining = options.limit - emitted
                    if remaining <= 0:
                        return
                    if chunk.height > remaining:
                        chunk = chunk.head(remaining)
                emitted += chunk.height
                yield chunk
                if options.limit is not None and emitted >= options.limit:
                    return

    def _read_file(self, path: Path, options: ReadOptions) -> Iterator[pl.DataFrame]:
        fmt = self._format_for(path)
        chunk_rows = max(options.chunk_rows, 1_000)
        try:
            if fmt is DataFormat.PARQUET:
                yield from self._read_parquet(path, chunk_rows)
            elif fmt in (DataFormat.CSV, DataFormat.TSV):
                yield from self._read_csv(path, fmt, chunk_rows)
            elif fmt is DataFormat.NDJSON:
                yield from _slice(pl.read_ndjson(self._open_bytes(path)), chunk_rows)
            elif fmt is DataFormat.JSON:
                yield from _slice(self._read_json(path), chunk_rows)
            elif fmt is DataFormat.XML:
                yield from _slice(self._read_xml(path), chunk_rows)
            elif fmt is DataFormat.EXCEL:
                yield from _slice(self._read_excel(path), chunk_rows)
            elif fmt is DataFormat.ARROW:
                yield from _slice(pl.read_ipc(path), chunk_rows)
            else:
                raise UnsupportedOperationError(
                    f"format '{fmt}' is not supported by this connector"
                )
        except (ConnectorError, UnsupportedOperationError):
            raise
        except Exception as exc:
            raise ConnectorError(
                f"failed reading {path.name}: {exc}",
                details={"path": str(path), "format": str(fmt)},
                cause=exc,
            ) from exc

    def _read_parquet(self, path: Path, chunk_rows: int) -> Iterator[pl.DataFrame]:
        lazy = pl.scan_parquet(path)
        total = lazy.select(pl.len()).collect().item()
        for offset in range(0, max(total, 1), chunk_rows):
            frame = lazy.slice(offset, chunk_rows).collect()
            if frame.is_empty():
                break
            yield frame

    def _read_csv(self, path: Path, fmt: DataFormat, chunk_rows: int) -> Iterator[pl.DataFrame]:
        kwargs: dict[str, Any] = {
            "separator": self._separator(fmt),
            "has_header": self.config.get("has_header", True),
            "infer_schema_length": self.config.get("infer_schema_rows", 10_000),
            "try_parse_dates": self.config.get("parse_dates", True),
            "null_values": self.config.get("null_values"),
            "ignore_errors": self.config.get("ignore_errors", False),
            "encoding": self.config.get("encoding", "utf8-lossy"),
        }
        if path.suffix.lower() in _COMPRESSED:
            # compressed payloads are decompressed in memory, then row-sliced
            yield from _slice(pl.read_csv(self._open_bytes(path), **kwargs), chunk_rows)
            return
        reader = pl.read_csv_batched(path, batch_size=chunk_rows, **kwargs)
        while True:
            batches = reader.next_batches(1)
            if not batches:
                return
            yield from batches

    def _read_json(self, path: Path) -> pl.DataFrame:
        payload = self._open_bytes(path)
        record_path = self.config.get("record_path")
        if record_path:
            import json

            data: Any = json.loads(payload.getvalue())
            for part in str(record_path).split("."):
                if part:
                    data = data[part] if isinstance(data, dict) else data
            if not isinstance(data, list):
                raise ConnectorError(f"record_path '{record_path}' did not resolve to a list")
            return pl.DataFrame(data, infer_schema_length=None, strict=False)
        return pl.read_json(payload)

    def _read_xml(self, path: Path) -> pl.DataFrame:
        """Flatten repeating XML elements into rows (attributes + leaf text, dotted paths).

        XML is parsed defensively: a document type declaration is refused outright, which blocks
        both external entity expansion (XXE) and internal entity bombs ("billion laughs") without
        needing a third-party parser. Payload size is bounded by ``max_bytes``.
        """
        payload = self._open_bytes(path).getvalue()
        limit = int(self.config.get("max_bytes", 256 * 1024 * 1024))
        if len(payload) > limit:
            raise ConnectorError(
                f"XML payload exceeds {limit} bytes",
                details={"path": str(path), "hint": "raise config['max_bytes'] deliberately"},
            )
        if re.search(rb"<!DOCTYPE", payload[:8192], re.IGNORECASE):
            raise ConnectorError(
                "XML document type declarations are refused",
                details={
                    "path": str(path),
                    "reason": "entity declarations enable XXE and expansion attacks",
                },
            )
        tree = ElementTree.parse(io.BytesIO(payload))  # noqa: S314 - DTD refused above
        root = tree.getroot()
        record_path = self.config.get("record_path")
        nodes = list(root.findall(record_path)) if record_path else list(root)
        if not nodes:
            raise ConnectorError(
                "no repeating element found in XML",
                details={"hint": "set config['record_path'], e.g. './row'"},
            )
        records = [_flatten_xml(node) for node in nodes]
        return pl.DataFrame(records, infer_schema_length=None, strict=False)

    def _read_excel(self, path: Path) -> pl.DataFrame:
        try:
            return pl.read_excel(
                path,
                sheet_name=self.config.get("sheet"),
                has_header=self.config.get("has_header", True),
            )
        except ImportError as exc:
            raise ConnectorError(
                "Excel support requires the 'excel' extra: pip install 'gdap[excel]'",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------ helpers
    def _open_bytes(self, path: Path) -> io.BytesIO:
        suffix = path.suffix.lower()
        if suffix == ".gz":
            return io.BytesIO(gzip.decompress(path.read_bytes()))
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                if not names:
                    raise ConnectorError(f"empty archive: {path.name}")
                member = self.config.get("member", names[0])
                return io.BytesIO(archive.read(member))
        if suffix == ".zst":
            try:
                import zstandard
            except ImportError as exc:  # pragma: no cover - optional
                raise ConnectorError(
                    "zstd files require the 'zstandard' package", cause=exc
                ) from exc
            return io.BytesIO(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
        return io.BytesIO(path.read_bytes())

    def _format_for(self, path: Path) -> DataFormat:
        if self.explicit_format:
            try:
                return DataFormat(self.explicit_format)
            except ValueError as exc:
                raise ConnectorError(
                    f"unknown format '{self.explicit_format}'",
                    details={"supported": [f.value for f in DataFormat]},
                ) from exc
        return detect_format(path)

    def _separator(self, fmt: DataFormat) -> str:
        return self.config.get("delimiter", "\t" if fmt is DataFormat.TSV else ",")

    def _resolve_one(self, options: ReadOptions) -> Path:
        if options.object:
            candidate = Path(options.object)
            if candidate.is_file():
                return candidate
            for path in self._files():
                if path.name == options.object or str(path) == options.object:
                    return path
            raise ConnectorError(
                f"object '{options.object}' not found in source '{self.name}'",
                details={"available": [p.name for p in self._files()[:50]]},
            )
        files = self._files()
        if not files:
            raise ConnectorError(f"no files matched for source '{self.name}'")
        return files[0]

    def _post_process(self, chunk: pl.DataFrame, options: ReadOptions) -> pl.DataFrame:
        if options.columns:
            present = [c for c in options.columns if c in chunk.columns]
            chunk = chunk.select(present)
        if options.incremental_column and options.since is not None:
            if options.incremental_column not in chunk.columns:
                raise ConnectorError(
                    f"incremental column '{options.incremental_column}' not present",
                    details={"columns": chunk.columns},
                )
            chunk = chunk.filter(pl.col(options.incremental_column) > options.since)
        return chunk


def _flatten_xml(node: ElementTree.Element, prefix: str = "") -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in node.attrib.items():
        record[f"{prefix}{key}"] = value
    for child in node:
        child_prefix = f"{prefix}{child.tag}."
        if len(child):
            record.update(_flatten_xml(child, child_prefix))
        else:
            record[f"{prefix}{child.tag}"] = (child.text or "").strip() or None
            for key, value in child.attrib.items():
                record[f"{child_prefix}{key}"] = value
    if not record and node.text:
        record[prefix.rstrip(".") or node.tag] = node.text.strip()
    return record


def _slice(frame: pl.DataFrame, chunk_rows: int) -> Iterator[pl.DataFrame]:
    if frame.height <= chunk_rows:
        yield frame
        return
    for offset in range(0, frame.height, chunk_rows):
        yield frame.slice(offset, chunk_rows)


class FilePlugin(ConnectorPlugin):
    key = "file"
    source_type = SourceType.FILE
    title = "Files (auto-detected format)"
    description = "Local or mounted files: CSV, TSV, JSON, NDJSON, Parquet, XML, Excel, Arrow."

    _format: str | None = None

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "file, directory or glob"},
                "pattern": {"type": "string", "default": "*"},
                "format": {"type": "string", "enum": [f.value for f in DataFormat]},
                "delimiter": {"type": "string"},
                "has_header": {"type": "boolean", "default": True},
                "encoding": {"type": "string", "default": "utf8-lossy"},
                "infer_schema_rows": {"type": "integer", "default": 10000},
                "parse_dates": {"type": "boolean", "default": True},
                "null_values": {"type": "array", "items": {"type": "string"}},
                "ignore_errors": {"type": "boolean", "default": False},
                "record_path": {"type": "string", "description": "JSON pointer path / XML XPath"},
                "sheet": {"type": "string", "description": "Excel sheet name"},
                "member": {"type": "string", "description": "member inside a .zip archive"},
            },
        }

    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> BaseConnector:
        if self._format and "format" not in spec.config:
            spec = spec.model_copy(update={"config": {**spec.config, "format": self._format}})
        return FileConnector(spec, secrets)


class CsvPlugin(FilePlugin):
    key = "file.csv"
    title = "CSV / delimited files"
    _format = DataFormat.CSV.value


class JsonPlugin(FilePlugin):
    key = "file.json"
    title = "JSON / NDJSON files"
    _format = None  # detected per file: .json vs .ndjson


class ParquetPlugin(FilePlugin):
    key = "file.parquet"
    title = "Parquet files"
    _format = DataFormat.PARQUET.value


class XmlPlugin(FilePlugin):
    key = "file.xml"
    title = "XML files"
    _format = DataFormat.XML.value
