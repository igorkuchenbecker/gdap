"""Small helpers around Polars frames shared by every engine.

Polars is the in-memory representation across the platform (ADR-002). These helpers keep the
conversion rules (schema extraction, JSON-safe records) in exactly one place.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import polars as pl

from gdap.core.contracts import ColumnSchema, DatasetSchema

_TEMPORAL = (pl.Date, pl.Datetime, pl.Time, pl.Duration)


def schema_from_frame(frame: pl.DataFrame | pl.LazyFrame, *, version: int = 1) -> DatasetSchema:
    """Structural schema (names/dtypes/nullability). Semantics are added by the profiler."""
    if isinstance(frame, pl.LazyFrame):
        schema = frame.collect_schema()
        columns = [
            ColumnSchema(name=name, dtype=str(dtype), nullable=True)
            for name, dtype in zip(schema.names(), schema.dtypes(), strict=True)
        ]
        return DatasetSchema(columns=columns, version=version)

    columns = []
    height = frame.height
    for name, dtype in frame.schema.items():
        nulls = frame[name].null_count() if height else 0
        columns.append(
            ColumnSchema(name=name, dtype=str(dtype), nullable=bool(nulls) or height == 0)
        )
    return DatasetSchema(columns=columns, version=version)


#: Polars 1.x exposes `DataType.is_numeric()`; the explicit tuple is the fallback for dtype
#: objects that predate it (and replaces `pl.NUMERIC_DTYPES`, removed in Polars 1.0).
_NUMERIC_DTYPES = (
    pl.Int8,
    pl.Int16,
    pl.Int32,
    pl.Int64,
    pl.UInt8,
    pl.UInt16,
    pl.UInt32,
    pl.UInt64,
    pl.Float32,
    pl.Float64,
    pl.Decimal,
)


def is_numeric(dtype: Any) -> bool:
    if hasattr(dtype, "is_numeric"):
        return bool(dtype.is_numeric())
    return dtype in _NUMERIC_DTYPES


def is_temporal(dtype: Any) -> bool:
    return dtype.is_temporal() if hasattr(dtype, "is_temporal") else isinstance(dtype, _TEMPORAL)


def numeric_columns(frame: pl.DataFrame) -> list[str]:
    return [name for name, dtype in frame.schema.items() if is_numeric(dtype)]


def temporal_columns(frame: pl.DataFrame) -> list[str]:
    return [name for name, dtype in frame.schema.items() if is_temporal(dtype)]


def categorical_columns(frame: pl.DataFrame, *, max_cardinality: int = 100) -> list[str]:
    out = []
    for name, dtype in frame.schema.items():
        if dtype in (pl.Utf8, pl.Categorical, pl.Enum, pl.Boolean) and (
            frame.height and frame[name].n_unique() <= max_cardinality
        ):
            out.append(name)
    return out


def to_float(value: Any, default: float = 0.0) -> float:
    """Narrow a Polars scalar to a float.

    Polars returns a deliberately broad union from `min()`, `mean()`, `sum()` and friends. Rather
    than sprinkling `float(x or 0)` (which turns a legitimate 0 into a default and crashes on a
    date), every conversion goes through here.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # Decimal and numpy scalars
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    """Integer counterpart of :func:`to_float`."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def json_safe(value: Any) -> Any:
    """Convert a cell to something ``json.dumps`` accepts, losslessly where possible."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(v) for v in value]
    if isinstance(value, int | str | bool):
        return value
    return str(value)


def to_records(frame: pl.DataFrame, *, limit: int | None = None) -> list[dict[str, Any]]:
    """JSON-safe row dicts, bounded by ``limit`` (never dump an unbounded frame into an API)."""
    subset = frame.head(limit) if limit is not None else frame
    return [{k: json_safe(v) for k, v in row.items()} for row in subset.iter_rows(named=True)]


def require_columns(frame: pl.DataFrame, columns: list[str], *, context: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        from gdap.core.errors import ValidationFailedError

        raise ValidationFailedError(
            f"{context}: column(s) not found: {', '.join(missing)}",
            details={"missing": missing, "available": frame.columns},
        )


def estimated_bytes(frame: pl.DataFrame) -> int:
    try:
        return int(frame.estimated_size())
    except Exception:  # pragma: no cover - polars version differences
        return 0
