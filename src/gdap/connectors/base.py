"""Connector base classes.

A connector does exactly four things — ``test``, ``discover``, ``infer_schema``, ``read`` — and it
streams. Nothing about a specific vendor leaks past this boundary: the ingestion engine only ever
sees chunks of Polars frames.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

import polars as pl

from gdap.core.contracts import (
    ConnectionTestResult,
    DatasetSchema,
    DiscoveredObject,
    ReadOptions,
    SourceSpec,
)
from gdap.core.enums import SourceType
from gdap.core.errors import ConnectorError
from gdap.core.frames import schema_from_frame
from gdap.observability.logging import get_logger

log = get_logger(__name__)


class BaseConnector(ABC):
    """Common behaviour: timing, error wrapping, sample-based schema inference."""

    key: str = "base"
    source_type: SourceType = SourceType.FILE

    def __init__(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> None:
        self.spec = spec
        self.config = dict(spec.config)
        self._secrets = secrets or {}
        self.name = spec.name

    # -- required ---------------------------------------------------------------
    @abstractmethod
    def discover(self) -> list[DiscoveredObject]: ...

    @abstractmethod
    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]: ...

    # -- provided ---------------------------------------------------------------
    def test(self) -> ConnectionTestResult:
        started = time.perf_counter()
        try:
            objects = self.discover()
        except ConnectorError as exc:
            return ConnectionTestResult(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                message=exc.message,
                details=exc.details,
            )
        except Exception as exc:  # defensive: connectors talk to hostile systems
            return ConnectionTestResult(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ConnectionTestResult(
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            message=f"connected: {len(objects)} object(s) reachable",
            details={"objects": [o.name for o in objects[:25]], "total": len(objects)},
        )

    def infer_schema(self, options: ReadOptions) -> DatasetSchema:
        """Default: read a bounded sample and describe it."""
        sample_options = options.model_copy(update={"limit": options.limit or 1000})
        for chunk in self.read(sample_options):
            return schema_from_frame(chunk)
        return DatasetSchema()

    def secret(self, name: str, default: str | None = None) -> str:
        value = self._secrets.get(name, default)
        if value is None:
            raise ConnectorError(
                f"missing secret '{name}' for source '{self.name}'",
                details={"hint": f"add secret_refs['{name}'] = 'env:MY_VAR'"},
            )
        return value

    def require(self, key: str) -> Any:
        if key not in self.config:
            raise ConnectorError(
                f"connector '{self.key}' requires config['{key}']",
                details={"provided": sorted(self.config)},
            )
        return self.config[key]

    def close(self) -> None:  # noqa: B027 - an optional hook, not every connector holds resources
        """Release resources. Called by the ingestion engine in a finally block."""

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ConnectorPlugin(ABC):
    """Factory + config schema for one connector kind. Registered by key, e.g. ``file.csv``."""

    key: str
    source_type: SourceType
    title: str = ""
    description: str = ""

    @abstractmethod
    def config_schema(self) -> dict[str, Any]: ...

    @abstractmethod
    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> BaseConnector: ...

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source_type": self.source_type.value,
            "title": self.title or self.key,
            "description": self.description,
            "config_schema": self.config_schema(),
        }
