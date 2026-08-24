"""Automatic data classification (§45).

Classification drives masking, retention and access policy. It is inferred from the column's
semantic type and name, and can always be overridden explicitly by a data owner — inference is a
starting point, not an authority.
"""

from __future__ import annotations

import re

from gdap.core.contracts import ColumnSchema, DatasetSchema
from gdap.core.enums import DataClassification, SemanticType

_SENSITIVE_SEMANTICS = {
    SemanticType.NATIONAL_ID: DataClassification.SENSITIVE,
    SemanticType.EMAIL: DataClassification.RESTRICTED,
    SemanticType.PHONE: DataClassification.RESTRICTED,
    SemanticType.IP_ADDRESS: DataClassification.CONFIDENTIAL,
    SemanticType.GEO_COORDINATE: DataClassification.CONFIDENTIAL,
}

_NAME_PATTERNS: list[tuple[re.Pattern[str], DataClassification]] = [
    (
        re.compile(r"(?i)\b(ssn|cpf|cnpj|nif|tax_?id|passport|national_?id)\b"),
        DataClassification.SENSITIVE,
    ),
    (re.compile(r"(?i)(password|secret|token|api_?key|credential)"), DataClassification.SENSITIVE),
    (
        re.compile(r"(?i)(salary|wage|compensation|income|health|diagnosis|medical)"),
        DataClassification.RESTRICTED,
    ),
    (
        re.compile(r"(?i)(email|phone|mobile|address|birth|dob|gender|race)"),
        DataClassification.RESTRICTED,
    ),
    (
        re.compile(r"(?i)(customer|client|user|employee)_?(id|name)"),
        DataClassification.CONFIDENTIAL,
    ),
    (
        re.compile(r"(?i)(revenue|margin|cost|price|profit|forecast)"),
        DataClassification.CONFIDENTIAL,
    ),
]


def classify_column(column: ColumnSchema) -> DataClassification:
    """Highest classification suggested by semantics or naming, floored at the current value."""
    candidates = [column.classification]
    if column.semantic_type in _SENSITIVE_SEMANTICS:
        candidates.append(_SENSITIVE_SEMANTICS[column.semantic_type])
    for pattern, level in _NAME_PATTERNS:
        if pattern.search(column.name):
            candidates.append(level)
    return max(candidates, key=lambda c: c.rank)


def classify_schema(schema: DatasetSchema) -> DatasetSchema:
    """Return a copy with per-column classification filled in."""
    columns = [
        column.model_copy(update={"classification": classify_column(column)})
        for column in schema.columns
    ]
    return schema.model_copy(update={"columns": columns})


def dataset_classification(schema: DatasetSchema) -> DataClassification:
    """A dataset is as sensitive as its most sensitive column."""
    if not schema.columns:
        return DataClassification.INTERNAL
    return max((c.classification for c in schema.columns), key=lambda c: c.rank)
