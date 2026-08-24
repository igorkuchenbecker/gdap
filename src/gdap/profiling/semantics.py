"""Semantic type inference.

Structural types (``Int64``, ``String``) say how a value is stored; semantic types say what it
*means*. Meaning is what drives masking, validation, chart selection and the wording of insights,
so it is inferred once, here, from three signals: dtype, column name and a bounded value sample.
"""

from __future__ import annotations

import re
from typing import Any

import polars as pl

from gdap.core.enums import SemanticType

_PATTERNS: list[tuple[SemanticType, re.Pattern[str]]] = [
    (SemanticType.EMAIL, re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    (SemanticType.URL, re.compile(r"^https?://[^\s]+$")),
    (
        SemanticType.UUID,
        re.compile(
            r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
        ),
    ),
    (
        SemanticType.IP_ADDRESS,
        re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}$"),
    ),
    (SemanticType.PHONE, re.compile(r"^\+?[\d\s().-]{7,20}$")),
    (SemanticType.POSTAL_CODE, re.compile(r"^\d{4,5}(-\d{3,4})?$|^[A-Z]\d[A-Z]\s?\d[A-Z]\d$")),
]

_NAME_HINTS: list[tuple[SemanticType, re.Pattern[str]]] = [
    (
        SemanticType.IDENTIFIER,
        re.compile(r"(?i)(^id$|_id$|^id_|uuid|guid|code$|_code$|sku|barcode)"),
    ),
    (
        SemanticType.CURRENCY,
        re.compile(
            r"(?i)(price|revenue|amount|cost|salary|value|total|margin|profit|fee|balance|payment)"
        ),
    ),
    (SemanticType.PERCENTAGE, re.compile(r"(?i)(pct|percent|rate$|_rate|ratio|share)")),
    (
        SemanticType.QUANTITY,
        re.compile(r"(?i)(qty|quantity|count|units|volume|weight|size|length)"),
    ),
    (SemanticType.DATE, re.compile(r"(?i)(date$|_date|^dt_|day$)")),
    (SemanticType.DATETIME, re.compile(r"(?i)(datetime|timestamp|_at$|_ts$|time$)")),
    (SemanticType.COUNTRY, re.compile(r"(?i)(country|nation|iso_?country)")),
    (SemanticType.GEO_COORDINATE, re.compile(r"(?i)(lat|latitude|lon|lng|longitude|geo)")),
    (SemanticType.NATIONAL_ID, re.compile(r"(?i)(ssn|cpf|cnpj|nif|tax_?id|passport|national_?id)")),
    (SemanticType.EMAIL, re.compile(r"(?i)(email|e_mail|mail)")),
    (SemanticType.PHONE, re.compile(r"(?i)(phone|mobile|telefone|cell)")),
    (SemanticType.POSTAL_CODE, re.compile(r"(?i)(zip|postal|cep)")),
]

_SAMPLE_SIZE = 200
_MATCH_THRESHOLD = 0.85


def infer_semantic_type(
    series: pl.Series,
    *,
    distinct_ratio: float | None = None,
    is_unique: bool = False,
) -> SemanticType:
    """Infer meaning from dtype + name + values. Conservative: falls back to structure."""
    name = series.name
    dtype = series.dtype

    if dtype == pl.Boolean:
        return SemanticType.BOOLEAN
    if dtype == pl.Date:
        return SemanticType.DATE
    if dtype in (pl.Datetime, pl.Time):
        return SemanticType.DATETIME

    non_null = series.drop_nulls()
    if non_null.is_empty():
        return SemanticType.UNKNOWN

    if dtype.is_numeric():
        for semantic, pattern in _NAME_HINTS:
            if pattern.search(name) and semantic in {
                SemanticType.CURRENCY,
                SemanticType.PERCENTAGE,
                SemanticType.QUANTITY,
                SemanticType.IDENTIFIER,
                SemanticType.GEO_COORDINATE,
                SemanticType.NATIONAL_ID,
            }:
                if semantic is SemanticType.IDENTIFIER and not (
                    is_unique or (distinct_ratio or 0) > 0.9
                ):
                    continue
                return semantic
        if _looks_like_epoch(non_null):
            return SemanticType.TIMESTAMP
        if is_unique and _is_integral(non_null):
            return SemanticType.IDENTIFIER
        if distinct_ratio is not None and distinct_ratio < 0.02 and _is_integral(non_null):
            return SemanticType.ORDINAL
        return SemanticType.NUMERIC

    if dtype in (pl.Utf8, pl.Categorical, pl.Enum):
        sample = [str(v) for v in non_null.head(_SAMPLE_SIZE).to_list()]
        for semantic, pattern in _PATTERNS:
            hits = sum(1 for value in sample if pattern.match(value))
            if hits / max(len(sample), 1) >= _MATCH_THRESHOLD:
                return semantic
        for semantic, pattern in _NAME_HINTS:
            if pattern.search(name):
                if semantic in {SemanticType.DATE, SemanticType.DATETIME} and not _parses_as_date(
                    sample
                ):
                    continue
                return semantic
        if _parses_as_date(sample):
            return SemanticType.DATE
        if is_unique or (distinct_ratio or 0) > 0.95:
            return SemanticType.IDENTIFIER
        average_length = sum(len(v) for v in sample) / max(len(sample), 1)
        if (distinct_ratio or 1) <= 0.5 and average_length <= 64:
            return SemanticType.CATEGORICAL
        if sample and all(v.startswith(("{", "[")) for v in sample[:20]):
            return SemanticType.JSON_BLOB
        return SemanticType.FREE_TEXT

    return SemanticType.UNKNOWN


def _is_integral(series: pl.Series) -> bool:
    return series.dtype in (
        pl.Int8,
        pl.Int16,
        pl.Int32,
        pl.Int64,
        pl.UInt8,
        pl.UInt16,
        pl.UInt32,
        pl.UInt64,
    )


def _looks_like_epoch(series: pl.Series) -> bool:
    """Integers in the 2001–2035 epoch-seconds band, when the name hints at time."""
    if not _is_integral(series):
        return False
    if not re.search(r"(?i)(time|_at$|_ts$|epoch|timestamp)", series.name):
        return False
    try:
        low, high = series.min(), series.max()
    except Exception:  # pragma: no cover
        return False
    if not isinstance(low, int | float) or not isinstance(high, int | float):
        return False
    return float(low) >= 1_000_000_000 and float(high) <= 2_100_000_000


_DATE_FORMATS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}"),
    re.compile(r"^\d{2}/\d{2}/\d{4}"),
    re.compile(r"^\d{2}-\d{2}-\d{4}"),
    re.compile(r"^\d{4}/\d{2}/\d{2}"),
)


def _parses_as_date(sample: list[str]) -> bool:
    if not sample:
        return False
    hits = sum(1 for value in sample if any(p.match(value) for p in _DATE_FORMATS))
    return hits / len(sample) >= _MATCH_THRESHOLD


def enrich_schema_semantics(frame: pl.DataFrame, schema: Any, *, sample_rows: int = 10_000) -> Any:
    """Fill in semantic types (and therefore classification) from a bounded sample.

    Called at *ingestion* time, not only when profiling: a dataset must be usable — and
    correctly classified for masking — the moment it lands, otherwise every consumer (the agent,
    the planner, chart selection) falls back to guessing by position.
    """
    from gdap.governance.classification import classify_column

    sample = frame.head(sample_rows) if frame.height > sample_rows else frame
    columns = []
    for column in schema.columns:
        if column.name not in sample.columns:
            columns.append(column)
            continue
        series = sample[column.name]
        non_null = series.drop_nulls()
        distinct_ratio = (non_null.n_unique() / non_null.len()) if non_null.len() else 0.0
        semantic = infer_semantic_type(
            series,
            distinct_ratio=distinct_ratio,
            is_unique=bool(non_null.len()) and non_null.n_unique() == non_null.len(),
        )
        updated = column.model_copy(update={"semantic_type": semantic})
        columns.append(updated.model_copy(update={"classification": classify_column(updated)}))
    return schema.model_copy(update={"columns": columns})
