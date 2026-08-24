"""In-memory connector.

Not a mock: it is a real connector over an in-process table registry. It powers the demo
generator, deterministic tests and "analyse this frame I already have" flows without pretending
to be a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import polars as pl

from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.contracts import DiscoveredObject, ReadOptions, SourceSpec
from gdap.core.enums import SourceType
from gdap.core.errors import ConnectorError, NotFoundError


class MemoryRegistry:
    """Process-wide table registry keyed by ``namespace/table``."""

    _tables: ClassVar[dict[str, pl.DataFrame]] = {}

    @classmethod
    def put(cls, namespace: str, name: str, frame: pl.DataFrame) -> None:
        cls._tables[f"{namespace}/{name}"] = frame

    @classmethod
    def get(cls, namespace: str, name: str) -> pl.DataFrame:
        try:
            return cls._tables[f"{namespace}/{name}"]
        except KeyError as exc:
            raise NotFoundError(f"in-memory table '{namespace}/{name}' not registered") from exc

    @classmethod
    def names(cls, namespace: str) -> list[str]:
        prefix = f"{namespace}/"
        return sorted(k[len(prefix) :] for k in cls._tables if k.startswith(prefix))

    @classmethod
    def clear(cls, namespace: str | None = None) -> None:
        if namespace is None:
            cls._tables.clear()
            return
        for key in [k for k in cls._tables if k.startswith(f"{namespace}/")]:
            del cls._tables[key]


class MemoryConnector(BaseConnector):
    key = "memory"
    source_type = SourceType.MEMORY

    def __init__(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> None:
        super().__init__(spec, secrets)
        self.namespace = str(self.config.get("namespace", spec.name))

    def discover(self) -> list[DiscoveredObject]:
        objects = []
        for name in MemoryRegistry.names(self.namespace):
            frame = MemoryRegistry.get(self.namespace, name)
            objects.append(
                DiscoveredObject(
                    name=name,
                    kind="table",
                    location=f"memory://{self.namespace}/{name}",
                    estimated_rows=frame.height,
                )
            )
        return objects

    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]:
        name = options.object or self.config.get("table")
        if not name:
            available = MemoryRegistry.names(self.namespace)
            if len(available) != 1:
                raise ConnectorError(
                    "specify options.object or config['table']",
                    details={"available": available},
                )
            name = available[0]
        frame = MemoryRegistry.get(self.namespace, str(name))
        if options.columns:
            frame = frame.select([c for c in options.columns if c in frame.columns])
        if options.incremental_column and options.since is not None:
            frame = frame.filter(pl.col(options.incremental_column) > options.since)
        if options.limit is not None:
            frame = frame.head(options.limit)
        chunk_rows = max(options.chunk_rows, 1)
        for offset in range(0, max(frame.height, 1), chunk_rows):
            piece = frame.slice(offset, chunk_rows)
            if piece.is_empty() and offset > 0:
                break
            yield piece


class MemoryPlugin(ConnectorPlugin):
    key = "memory"
    source_type = SourceType.MEMORY
    title = "In-memory tables"
    description = "Frames registered in-process (demo data, tests, ad-hoc analysis)."

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": [],
            "properties": {
                "namespace": {"type": "string"},
                "table": {"type": "string"},
            },
        }

    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> BaseConnector:
        return MemoryConnector(spec, secrets)
