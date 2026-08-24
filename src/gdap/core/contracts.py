"""Data contracts — the type system of the platform (rule §35).

Modules never exchange loose dictionaries: a producer emits a contract, a consumer validates it.
A breaking change in a contract is therefore a *compile-time-ish* event, not a silent runtime bug.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gdap.core.enums import (
    AnalysisKind,
    ApprovalMode,
    DataClassification,
    DataFormat,
    IngestionMode,
    InsightKind,
    JobState,
    Permission,
    QualityDimension,
    ReportFormat,
    Role,
    SemanticType,
    Severity,
    SourceType,
    StepState,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    """Base contract: strict by default so typos fail loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


# ─────────────────────────────────────── identity & security ───────────────────────────────


class Principal(Contract):
    """Who is acting. Every service call carries one; there is no implicit ambient identity."""

    org_id: str
    user_id: str
    email: str | None = None
    role: Role = Role.VIEWER
    permissions: frozenset[Permission] = Field(default_factory=frozenset)
    api_key_id: str | None = None
    is_system: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True)

    def has(self, permission: Permission) -> bool:
        return Permission.ADMIN in self.permissions or permission in self.permissions

    @classmethod
    def system(cls, org_id: str, reason: str = "system") -> Principal:
        """Internal actor (scheduler, worker). Still tenant-scoped, still audited."""
        return cls(
            org_id=org_id,
            user_id=f"system:{reason}",
            role=Role.SERVICE,
            permissions=frozenset(Permission),
            is_system=True,
        )


# ───────────────────────────────────────────── schema ──────────────────────────────────────


class ColumnSchema(Contract):
    name: str
    dtype: str  # canonical polars dtype string
    nullable: bool = True
    semantic_type: SemanticType = SemanticType.UNKNOWN
    classification: DataClassification = DataClassification.INTERNAL
    description: str | None = None
    unit: str | None = None

    def fingerprint(self) -> str:
        return f"{self.name}:{self.dtype}:{'null' if self.nullable else 'notnull'}"


class DatasetSchema(Contract):
    columns: list[ColumnSchema] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    version: int = 1

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnSchema | None:
        return next((c for c in self.columns if c.name == name), None)

    def fingerprint(self) -> str:
        payload = "|".join(sorted(c.fingerprint() for c in self.columns))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def diff(self, other: DatasetSchema) -> SchemaDiff:
        mine = {c.name: c for c in self.columns}
        theirs = {c.name: c for c in other.columns}
        added = sorted(set(theirs) - set(mine))
        removed = sorted(set(mine) - set(theirs))
        changed = sorted(
            name
            for name in set(mine) & set(theirs)
            if mine[name].dtype != theirs[name].dtype
            or mine[name].nullable != theirs[name].nullable
        )
        return SchemaDiff(added=added, removed=removed, type_changed=changed)


class SchemaDiff(Contract):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    type_changed: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.type_changed)

    @property
    def is_breaking(self) -> bool:
        """Additive evolution is safe; dropping or retyping a column is not."""
        return bool(self.removed or self.type_changed)


# ─────────────────────────────────────────── connectors ────────────────────────────────────


class SourceSpec(Contract):
    """Declarative description of a source. Secrets appear only as references."""

    name: str
    type: SourceType
    connector: str  # registry key, e.g. "file.csv", "sql.postgres", "rest.json"
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)  # {"password": "env:PGPASS"}
    classification: DataClassification = DataClassification.INTERNAL
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _forbid_inline_secrets(self) -> SourceSpec:
        suspicious = {"password", "secret", "token", "api_key", "apikey", "private_key"}
        for key, value in self.config.items():
            if key.lower() in suspicious and isinstance(value, str) and ":" not in value:
                raise ValueError(
                    f"inline secret in config['{key}'] — use secret_refs with 'env:VAR' instead"
                )
        return self


class ConnectionTestResult(Contract):
    ok: bool
    latency_ms: float
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiscoveredObject(Contract):
    """One addressable unit inside a source: a file, a table, an endpoint."""

    name: str
    kind: Literal["file", "table", "view", "endpoint", "collection"]
    location: str
    format: DataFormat | None = None
    estimated_rows: int | None = None
    size_bytes: int | None = None
    schema_: DatasetSchema | None = Field(default=None, alias="schema")
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ReadOptions(Contract):
    """Everything a connector needs to stream one object out of a source."""

    object: str | None = None
    limit: int | None = None
    columns: list[str] | None = None
    chunk_rows: int = 250_000
    mode: IngestionMode = IngestionMode.FULL
    incremental_column: str | None = None
    since: Any | None = None
    query: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────── ingestion ─────────────────────────────────────


class IngestionResult(Contract):
    dataset: str
    dataset_id: str
    version: int
    source: str
    mode: IngestionMode
    rows: int
    columns: int
    bytes_written: int
    checksum: str
    storage_uri: str
    schema_: DatasetSchema = Field(alias="schema")
    schema_diff: SchemaDiff | None = None
    started_at: datetime
    finished_at: datetime
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


# ─────────────────────────────────────────── profiling ─────────────────────────────────────


class NumericStats(Contract):
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    variance: float | None = None
    skewness: float | None = None
    kurtosis: float | None = None
    p01: float | None = None
    p05: float | None = None
    p25: float | None = None
    p75: float | None = None
    p95: float | None = None
    p99: float | None = None
    zeros: int = 0
    negatives: int = 0
    outlier_count: int = 0
    outlier_bounds: tuple[float, float] | None = None
    histogram: list[tuple[float, float, int]] = Field(default_factory=list)


class TemporalStats(Contract):
    min: datetime | None = None
    max: datetime | None = None
    span_days: float | None = None
    inferred_granularity: str | None = None
    gaps: int = 0
    future_values: int = 0


class TextStats(Contract):
    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None
    empty_strings: int = 0
    whitespace_issues: int = 0
    detected_patterns: list[str] = Field(default_factory=list)


class ColumnProfile(Contract):
    name: str
    dtype: str
    semantic_type: SemanticType = SemanticType.UNKNOWN
    classification: DataClassification = DataClassification.INTERNAL
    count: int = 0
    null_count: int = 0
    null_ratio: float = 0.0
    distinct_count: int = 0
    distinct_ratio: float = 0.0
    is_constant: bool = False
    is_unique: bool = False
    is_candidate_key: bool = False
    top_values: list[tuple[Any, int]] = Field(default_factory=list)
    numeric: NumericStats | None = None
    temporal: TemporalStats | None = None
    text: TextStats | None = None
    sample_values: list[Any] = Field(default_factory=list)


class RelationshipHint(Contract):
    left_column: str
    right_dataset: str
    right_column: str
    kind: Literal["foreign_key", "shared_domain"] = "foreign_key"
    overlap_ratio: float = 0.0
    confidence: float = 0.0


class DatasetProfile(Contract):
    dataset: str
    dataset_version_id: str | None = None
    rows: int
    columns: int
    memory_bytes: int = 0
    schema_: DatasetSchema = Field(alias="schema")
    column_profiles: list[ColumnProfile] = Field(default_factory=list)
    duplicate_rows: int = 0
    duplicate_ratio: float = 0.0
    candidate_keys: list[list[str]] = Field(default_factory=list)
    relationships: list[RelationshipHint] = Field(default_factory=list)
    correlations: dict[str, dict[str, float]] = Field(default_factory=dict)
    recommendations: list[str] = Field(default_factory=list)
    profiled_at: datetime = Field(default_factory=utcnow)
    sampled: bool = False
    sample_rows: int | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.column_profiles if c.name == name), None)


# ───────────────────────────────────────────── quality ─────────────────────────────────────


class Expectation(Contract):
    """A declarative, testable statement about data (rule §27 data tests)."""

    column: str | None = None
    kind: Literal[
        "not_null",
        "unique",
        "in_range",
        "in_set",
        "matches_regex",
        "of_type",
        "row_count_between",
        "freshness",
        "custom_sql",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = Severity.CRITICAL
    dimension: QualityDimension = QualityDimension.VALIDITY
    description: str | None = None

    def label(self) -> str:
        target = self.column or "<dataset>"
        return f"{self.kind}({target})"


class QualityFinding(Contract):
    dimension: QualityDimension
    severity: Severity
    column: str | None = None
    rule: str
    message: str
    failed_rows: int = 0
    failed_ratio: float = 0.0
    sample: list[Any] = Field(default_factory=list)
    suggestion: str | None = None


class DimensionScore(Contract):
    dimension: QualityDimension
    score: float
    weight: float
    checks: int = 0
    failed: int = 0


class QualityReport(Contract):
    dataset: str
    dataset_version_id: str | None = None
    score: float
    status: Literal["pass", "warn", "fail"]
    dimensions: list[DimensionScore] = Field(default_factory=list)
    findings: list[QualityFinding] = Field(default_factory=list)
    rows_checked: int = 0
    expectations_evaluated: int = 0
    evaluated_at: datetime = Field(default_factory=utcnow)

    @property
    def critical(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.severity == Severity.CRITICAL]


# ───────────────────────────────────────────── cleaning ────────────────────────────────────


class CleaningProposal(Contract):
    """Never applied silently: a proposal is data, applying it is an audited decision."""

    id: str
    column: str | None
    issue: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["deterministic", "ai_suggested"] = "deterministic"
    approval: ApprovalMode = ApprovalMode.AUTO
    affected_rows: int = 0
    reversible: bool = True
    confidence: float = 1.0
    rationale: str = ""


class CleaningResult(Contract):
    applied: list[CleaningProposal] = Field(default_factory=list)
    skipped: list[CleaningProposal] = Field(default_factory=list)
    rows_before: int = 0
    rows_after: int = 0
    cells_changed: int = 0
    log: list[str] = Field(default_factory=list)


# ───────────────────────────────────────────── analytics ───────────────────────────────────


class Evidence(Contract):
    """AI safety §12: no claim without traceable evidence."""

    source: str
    query: str | None = None
    calculation: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    rows_considered: int | None = None


class Insight(Contract):
    kind: InsightKind
    title: str
    detail: str
    severity: Severity = Severity.INFO
    confidence: float = 1.0
    evidence: list[Evidence] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _facts_need_evidence(self) -> Insight:
        if self.kind == InsightKind.FACT and not self.evidence:
            raise ValueError("a FACT insight must carry evidence")
        return self


class ChartSpec(Contract):
    kind: Literal["line", "bar", "hbar", "scatter", "histogram", "box", "heatmap", "pie", "area"]
    title: str
    x: str | None = None
    y: str | list[str] | None = None
    series: str | None = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(Contract):
    kind: AnalysisKind
    dataset: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    charts: list[ChartSpec] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# ───────────────────────────────────────────── pipelines ───────────────────────────────────


class StepSpec(Contract):
    id: str | None = None
    uses: str  # registry key, e.g. "transform.calculate"
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    input: str | None = None  # frame name; defaults to the previous step's output
    output: str | None = None  # frame name to publish
    when: str | None = None  # optional guard expression over params/metrics
    approval: ApprovalMode | None = None
    continue_on_error: bool = False
    description: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @classmethod
    def of(
        cls,
        uses: str,
        *,
        id: str | None = None,  # noqa: A002 - mirrors the YAML field name
        input: str | None = None,  # noqa: A002
        output: str | None = None,
        when: str | None = None,
        approval: ApprovalMode | None = None,
        options: dict[str, Any] | None = None,
    ) -> StepSpec:
        """Build a step programmatically.

        ``with`` is a Python keyword, so the field is aliased. Constructing through
        ``StepSpec(**{"with": …})`` works but defeats type checking at every call site; this
        keeps the ergonomics *and* the types.
        """
        return cls.model_validate(
            {
                "uses": uses,
                "id": id,
                "input": input,
                "output": output,
                "when": when,
                "approval": approval,
                "with": options or {},
            }
        )


class RetryPolicy(Contract):
    max_attempts: int = 3
    backoff_seconds: float = 5.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 300.0


class ScheduleSpec(Contract):
    cron: str | None = None
    every: str | None = None  # "5m", "1h", "daily", "weekly"
    timezone: str = "UTC"
    enabled: bool = True

    @model_validator(mode="after")
    def _one_of(self) -> ScheduleSpec:
        if bool(self.cron) == bool(self.every):
            raise ValueError("schedule requires exactly one of 'cron' or 'every'")
        return self


class PipelineSpec(Contract):
    """The declarative unit of automation (§10). Versioned, reviewable, reproducible."""

    name: str
    version: int = 1
    description: str | None = None
    owner: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    schedule: ScheduleSpec | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    on_failure: Literal["stop", "continue"] = "stop"
    depends_on: list[str] = Field(default_factory=list)
    quality_gate: float | None = None  # abort if quality score drops below this
    steps: list[StepSpec] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class StepResult(Contract):
    step_id: str
    uses: str
    state: StepState
    started_at: datetime
    finished_at: datetime | None = None
    rows_in: int | None = None
    rows_out: int | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None

    @property
    def duration_ms(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds() * 1000


class JobResult(Contract):
    job_id: str
    pipeline: str
    pipeline_version: int
    state: JobState
    started_at: datetime
    finished_at: datetime | None = None
    steps: list[StepResult] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    attempt: int = 1


# ───────────────────────────────────────────── reporting ───────────────────────────────────


class ReportSection(Contract):
    title: str
    body: str | None = None
    table: list[dict[str, Any]] | None = None
    charts: list[ChartSpec] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    level: int = 2


class ReportSpec(Contract):
    title: str
    subtitle: str | None = None
    formats: list[ReportFormat] = Field(default_factory=lambda: [ReportFormat.HTML])
    sections: list[ReportSection] = Field(default_factory=list)
    executive_summary: str | None = None
    kpis: list[dict[str, Any]] = Field(default_factory=list)
    methodology: str | None = None
    locale: str = "en_US"
    timezone: str = "UTC"
    generated_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────────────────── governance & ops ────────────────────────────────


class LineageEdge(Contract):
    upstream_type: str
    upstream_id: str
    downstream_type: str
    downstream_id: str
    operation: str
    job_id: str | None = None
    at: datetime = Field(default_factory=utcnow)


class AuditEvent(Contract):
    actor: str
    action: str
    resource_type: str
    resource_id: str | None = None
    result: Literal["success", "denied", "error"] = "success"
    details: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)
    trace_id: str | None = None


class AlertSpec(Contract):
    rule: str
    severity: Severity
    title: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    channels: list[str] = Field(default_factory=lambda: ["log", "store"])
    dedupe_key: str | None = None


class HealthCheck(Contract):
    component: str
    ok: bool
    message: str = ""
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SystemHealth(Contract):
    ok: bool
    version: str
    environment: str
    checks: list[HealthCheck] = Field(default_factory=list)
    at: datetime = Field(default_factory=utcnow)


# ───────────────────────────────────────────── ai layer ────────────────────────────────────


class ToolSpec(Contract):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    required_permissions: list[Permission] = Field(default_factory=list)
    approval: ApprovalMode = ApprovalMode.AUTO
    read_only: bool = True


class ToolCall(Contract):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


class ToolResult(Contract):
    tool: str
    call_id: str | None = None
    ok: bool
    content: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    evidence: Evidence | None = None


class AgentAnswer(Contract):
    question: str
    answer: str
    insights: list[Insight] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    confidence: float = 0.0
    provider: str = "heuristic"
    limitations: list[str] = Field(default_factory=list)


class PipelinePlan(Contract):
    """Natural language → reviewable pipeline (§41). Never auto-executed by default."""

    request: str
    spec: PipelineSpec
    rationale: str
    assumptions: list[str] = Field(default_factory=list)
    requires_review: bool = True
    provider: str = "heuristic"
    confidence: float = 0.0
