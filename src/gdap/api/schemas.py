"""Request bodies for the HTTP API.

Responses are the platform's own contracts (:mod:`gdap.core.contracts`) or plain dictionaries
built by the services' ``to_dict`` helpers — the API adds no second modelling layer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gdap.core.contracts import Expectation, PipelineSpec, SourceSpec
from gdap.core.enums import AnalysisKind, DataClassification, IngestionMode, ReportFormat


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSourceRequest(Body):
    name: str
    type: str
    connector: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    classification: DataClassification = DataClassification.INTERNAL
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    def to_spec(self) -> SourceSpec:
        return SourceSpec(
            name=self.name,
            type=self.type,  # type: ignore[arg-type]
            connector=self.connector,
            config=self.config,
            secret_refs=self.secret_refs,
            classification=self.classification,
            description=self.description,
            tags=self.tags,
        )


class IngestBody(Body):
    object: str | None = None
    dataset: str | None = None
    mode: IngestionMode = IngestionMode.FULL
    incremental_column: str | None = None
    dedupe_keys: list[str] = Field(default_factory=list)
    columns: list[str] | None = None
    limit: int | None = None
    query: str | None = None
    async_: bool = Field(default=False, alias="async")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class QueryBody(Body):
    sql: str
    limit: int | None = Field(default=None, ge=1, le=100_000)
    datasets: list[str] | None = None


class ValidateBody(Body):
    version: int | None = None
    expectations: list[Expectation] = Field(default_factory=list)
    auto_expectations: bool = True


class CleaningBody(Body):
    version: int | None = None
    approve: list[str] = Field(default_factory=list)
    apply: bool = False


class AnalysisBody(Body):
    dataset: str
    kind: AnalysisKind
    params: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    persist: bool = True


class ReportBody(Body):
    dataset: str
    title: str | None = None
    formats: list[ReportFormat] = Field(default_factory=lambda: [ReportFormat.HTML])
    kinds: list[AnalysisKind] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    include_profile: bool = True
    include_quality: bool = True


class CreatePipelineBody(Body):
    """Accepts a structured spec or a raw YAML document."""

    spec: PipelineSpec | None = None
    yaml: str | None = None


class RunPipelineBody(Body):
    params: dict[str, Any] = Field(default_factory=dict)
    wait: bool = False


class ApproveBody(Body):
    steps: list[str] = Field(default_factory=list)
    note: str | None = None


class RejectBody(Body):
    reason: str


class AskBody(Body):
    question: str
    dataset: str | None = None
    agent: str | None = None
    approved_tools: list[str] = Field(default_factory=list)


class PlanBody(Body):
    request: str
    dataset: str | None = None
    create: bool = False


class CreateApiKeyBody(Body):
    name: str
    user_email: str | None = None
    role: Literal["owner", "admin", "engineer", "analyst", "viewer"] = "analyst"
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = None


class AlertAckBody(Body):
    note: str | None = None
