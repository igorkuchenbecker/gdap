"""Ports (hexagonal architecture).

These protocols are the only thing the application layer is allowed to depend on. Every concrete
technology — DuckDB, the local filesystem, S3, Anthropic, PostgreSQL — is an *adapter* bound in
:mod:`gdap.core.container`. Swapping an adapter must never require touching a service.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    import polars as pl

    from gdap.core.contracts import (
        AlertSpec,
        ConnectionTestResult,
        DatasetSchema,
        DiscoveredObject,
        ReadOptions,
        SourceSpec,
        ToolSpec,
    )


@runtime_checkable
class Connector(Protocol):
    """Reads data out of one kind of system. Stateless between calls; owns no global state."""

    name: str

    def test(self) -> ConnectionTestResult:
        """Probe connectivity and permissions without moving data."""

    def discover(self) -> list[DiscoveredObject]:
        """List addressable objects (files, tables, endpoints) with inferred schemas."""

    def infer_schema(self, options: ReadOptions) -> DatasetSchema:
        """Return the schema of one object without reading it fully."""

    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]:
        """Stream the object in bounded chunks (never load everything in memory)."""


@runtime_checkable
class ConnectorFactory(Protocol):
    """Builds a connector from a validated :class:`SourceSpec` plus resolved secrets."""

    key: str
    source_type: Any

    def config_schema(self) -> dict[str, Any]:
        """JSON schema used to validate ``SourceSpec.config`` before instantiation."""

    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> Connector: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Artifact/dataset byte storage. Local FS today, object storage tomorrow."""

    scheme: str

    def write_bytes(self, key: str, payload: bytes) -> str: ...

    def read_bytes(self, key: str) -> bytes: ...

    def write_file(self, key: str, path: Path) -> str: ...

    def local_path(self, key: str) -> Path:
        """Materialise the object locally (a no-op for the local adapter)."""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> list[str]: ...

    def uri(self, key: str) -> str: ...


@runtime_checkable
class QueryEngine(Protocol):
    """Executes analytical SQL over registered datasets under a safety policy."""

    def query(
        self, sql: str, *, params: dict[str, Any] | None = None, limit: int | None = None
    ) -> pl.DataFrame: ...

    def register(self, name: str, source: Any) -> None: ...

    def explain(self, sql: str) -> str: ...


@runtime_checkable
class JobQueue(Protocol):
    """At-least-once job distribution. DB-backed now; Celery/Temporal later (ADR-004)."""

    def enqueue(self, job_id: str, *, priority: int = 0) -> None: ...

    def lease(self, worker_id: str, *, lease_seconds: int) -> str | None: ...

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int) -> None: ...

    def complete(self, job_id: str) -> None: ...

    def fail(self, job_id: str, error: str, *, retry_in_seconds: float | None = None) -> None: ...


@runtime_checkable
class NotificationChannel(Protocol):
    """One delivery mechanism for alerts."""

    name: str

    def send(self, alert: AlertSpec) -> bool: ...


@runtime_checkable
class SecretResolver(Protocol):
    """Turns a reference (``env:VAR``, ``file:/path``) into a value, at the last moment."""

    def resolve(self, ref: str) -> str: ...

    def resolve_all(self, refs: dict[str, str]) -> dict[str, str]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Provider-agnostic LLM access. Implementations must never raise raw vendor exceptions."""

    name: str
    supports_tools: bool

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Return ``{"text": str, "tool_calls": [...], "usage": {...}, "stop_reason": str}``."""


@runtime_checkable
class ModelBackend(Protocol):
    """Training/inference abstraction so ML never leaks into the ETL layer (§24)."""

    task: str

    def fit(
        self,
        frame: pl.DataFrame,
        *,
        target: str | None,
        features: list[str],
        params: dict[str, Any],
    ) -> dict[str, Any]: ...

    def predict(self, frame: pl.DataFrame) -> pl.DataFrame: ...

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> ModelBackend: ...


@runtime_checkable
class MetricsSink(Protocol):
    def increment(self, name: str, value: float = 1.0, **labels: str) -> None: ...

    def observe(self, name: str, value: float, **labels: str) -> None: ...

    def gauge(self, name: str, value: float, **labels: str) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...
