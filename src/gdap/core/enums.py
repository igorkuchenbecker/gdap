"""Closed vocabularies shared by every layer of the platform."""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    FILE = "file"
    SQL = "sql"
    REST = "rest"
    OBJECT_STORAGE = "object_storage"
    MEMORY = "memory"


class DataFormat(StrEnum):
    CSV = "csv"
    TSV = "tsv"
    JSON = "json"
    NDJSON = "ndjson"
    XML = "xml"
    EXCEL = "excel"
    PARQUET = "parquet"
    AVRO = "avro"
    ARROW = "arrow"


class IngestionMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    APPEND = "append"
    STREAM = "stream"


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLED = "CANCELLED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.SUCCESS, JobState.FAILED, JobState.CANCELLED}


class StepState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


class TriggerType(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    EVENT = "event"
    API = "api"
    DEPENDENCY = "dependency"
    AGENT = "agent"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class QualityDimension(StrEnum):
    COMPLETENESS = "completeness"
    ACCURACY = "accuracy"
    CONSISTENCY = "consistency"
    VALIDITY = "validity"
    UNIQUENESS = "uniqueness"
    TIMELINESS = "timeliness"
    INTEGRITY = "integrity"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    SENSITIVE = "SENSITIVE"

    @property
    def rank(self) -> int:
        order = {
            "PUBLIC": 0,
            "INTERNAL": 1,
            "CONFIDENTIAL": 2,
            "RESTRICTED": 3,
            "SENSITIVE": 4,
        }
        return order[self.value]


class ApprovalMode(StrEnum):
    """Human-in-the-loop levels (see docs/GOVERNANCE.md)."""

    AUTO = "AUTO"
    AUTO_WITH_VALIDATION = "AUTO_WITH_VALIDATION"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    BLOCKED = "BLOCKED"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ENGINEER = "engineer"
    ANALYST = "analyst"
    VIEWER = "viewer"
    SERVICE = "service"


class Permission(StrEnum):
    SOURCE_READ = "source:read"
    SOURCE_WRITE = "source:write"
    DATASET_READ = "dataset:read"
    DATASET_WRITE = "dataset:write"
    PIPELINE_READ = "pipeline:read"
    PIPELINE_WRITE = "pipeline:write"
    PIPELINE_RUN = "pipeline:run"
    JOB_READ = "job:read"
    JOB_WRITE = "job:write"
    ANALYSIS_RUN = "analysis:run"
    REPORT_READ = "report:read"
    REPORT_WRITE = "report:write"
    AGENT_USE = "agent:use"
    SQL_WRITE = "sql:write"
    SQL_DESTRUCTIVE = "sql:destructive"
    GOVERNANCE_READ = "governance:read"
    ADMIN = "admin:*"


class SemanticType(StrEnum):
    """Inferred meaning of a column — drives masking, validation and chart selection."""

    UNKNOWN = "unknown"
    IDENTIFIER = "identifier"
    CATEGORICAL = "categorical"
    ORDINAL = "ordinal"
    NUMERIC = "numeric"
    CURRENCY = "currency"
    PERCENTAGE = "percentage"
    QUANTITY = "quantity"
    DATE = "date"
    DATETIME = "datetime"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    IP_ADDRESS = "ip_address"
    UUID = "uuid"
    POSTAL_CODE = "postal_code"
    COUNTRY = "country"
    GEO_COORDINATE = "geo_coordinate"
    NATIONAL_ID = "national_id"
    FREE_TEXT = "free_text"
    JSON_BLOB = "json_blob"


class AnalysisKind(StrEnum):
    DESCRIBE = "describe"
    CORRELATION = "correlation"
    SEGMENTATION = "segmentation"
    COMPARISON = "comparison"
    TREND = "trend"
    ANOMALY = "anomaly"
    FORECAST = "forecast"
    DRIVERS = "drivers"


class ReportFormat(StrEnum):
    HTML = "html"
    PDF = "pdf"
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"
    MARKDOWN = "markdown"


class AlertChannel(StrEnum):
    LOG = "log"
    WEBHOOK = "webhook"
    EMAIL = "email"
    STORE = "store"


class InsightKind(StrEnum):
    """AI safety requirement §36.7: fact / inference / hypothesis / recommendation."""

    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    RECOMMENDATION = "recommendation"
