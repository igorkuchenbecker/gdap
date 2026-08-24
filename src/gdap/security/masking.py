"""Column masking and anonymisation (§46).

Masking is applied on the way *out* (previews, reports, agent tool results) based on the column's
inferred semantics and classification — never by mutating stored data.
"""

from __future__ import annotations

import hashlib
import re

import polars as pl

from gdap.core.contracts import ColumnSchema, DatasetSchema
from gdap.core.enums import DataClassification, SemanticType

SENSITIVE_SEMANTICS = {
    SemanticType.EMAIL,
    SemanticType.PHONE,
    SemanticType.NATIONAL_ID,
    SemanticType.IP_ADDRESS,
}

_EMAIL = re.compile(r"^([^@]{1,3})[^@]*(@.*)$")


def should_mask(column: ColumnSchema, *, min_classification: DataClassification) -> bool:
    return (
        column.classification.rank >= min_classification.rank
        or column.semantic_type in SENSITIVE_SEMANTICS
    )


def mask_value(value: str | None, semantic: SemanticType) -> str | None:
    if value is None:
        return None
    if semantic is SemanticType.EMAIL:
        return _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", value)
    if semantic in (SemanticType.PHONE, SemanticType.NATIONAL_ID):
        keep = value[-2:] if len(value) > 2 else ""
        return "*" * max(len(value) - 2, 0) + keep
    if len(value) <= 2:
        return "**"
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"


def pseudonymize(value: str | None, *, salt: str) -> str | None:
    """Stable pseudonym: same input + salt → same token, original not recoverable."""
    if value is None:
        return None
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def apply_masking(
    frame: pl.DataFrame,
    schema: DatasetSchema,
    *,
    min_classification: DataClassification = DataClassification.RESTRICTED,
    enabled: bool = True,
) -> pl.DataFrame:
    """Return a copy with sensitive columns masked. Non-destructive by construction."""
    if not enabled:
        return frame
    expressions = []
    for column in schema.columns:
        if column.name not in frame.columns:
            continue
        if not should_mask(column, min_classification=min_classification):
            continue
        semantic = column.semantic_type
        expressions.append(
            pl.col(column.name)
            .cast(pl.Utf8)
            .map_elements(
                lambda value, meaning=semantic: mask_value(value, meaning),  # type: ignore[misc]
                return_dtype=pl.Utf8,
            )
            .alias(column.name)
        )
    return frame.with_columns(expressions) if expressions else frame
