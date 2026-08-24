"""SQL database connector (any SQLAlchemy dialect).

One connector covers PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite and warehouses that
ship a SQLAlchemy dialect. Rows are streamed with ``fetchmany`` so a billion-row table never
becomes a billion-row Python list, and incremental reads are pushed down to a ``WHERE`` clause.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

import polars as pl
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from gdap.connectors.base import BaseConnector, ConnectorPlugin
from gdap.core.contracts import (
    ColumnSchema,
    DatasetSchema,
    DiscoveredObject,
    ReadOptions,
    SourceSpec,
)
from gdap.core.enums import SourceType
from gdap.core.errors import ConnectorError
from gdap.core.frames import schema_from_frame
from gdap.observability.logging import get_logger
from gdap.security.sql_guard import SqlPolicy, guard

log = get_logger(__name__)

_DRIVERS = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "sqlserver": "mssql+pyodbc",
    "mssql": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
    "sqlite": "sqlite",
}


class SqlConnector(BaseConnector):
    """Config: either ``url``, or ``driver``+``host``+``database`` (+ ``secret_refs.password``)."""

    key = "sql"
    source_type = SourceType.SQL

    def __init__(self, spec: SourceSpec, secrets: dict[str, str] | None = None) -> None:
        super().__init__(spec, secrets)
        self._engine: Engine | None = None
        self.schema_name: str | None = self.config.get("schema")

    # ------------------------------------------------------------------ connection
    def _url(self) -> str:
        if "url" in self._secrets:
            return self._secrets["url"]
        if "url" in self.config:
            return str(self.config["url"])

        driver_key = str(self.require("driver")).lower()
        driver = _DRIVERS.get(driver_key, driver_key)
        database = self.require("database")
        if driver == "sqlite":
            return f"sqlite:///{database}"
        host = self.require("host")
        port = self.config.get("port")
        user = self.config.get("username", "")
        password = self._secrets.get("password", "")
        credentials = ""
        if user:
            credentials = quote_plus(str(user))
            if password:
                credentials += f":{quote_plus(str(password))}"
            credentials += "@"
        netloc = f"{host}:{port}" if port else str(host)
        return f"{driver}://{credentials}{netloc}/{database}"

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            try:
                self._engine = create_engine(
                    self._url(),
                    pool_pre_ping=True,
                    connect_args=self.config.get("connect_args", {}),
                )
            except Exception as exc:
                raise ConnectorError(
                    f"could not create engine for source '{self.name}': {exc}", cause=exc
                ) from exc
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------------ discovery
    def discover(self) -> list[DiscoveredObject]:
        try:
            inspector = inspect(self.engine)
            objects: list[DiscoveredObject] = []
            for kind, names in (
                ("table", inspector.get_table_names(schema=self.schema_name)),
                ("view", inspector.get_view_names(schema=self.schema_name)),
            ):
                for name in names:
                    columns = inspector.get_columns(name, schema=self.schema_name)
                    schema = DatasetSchema(
                        columns=[
                            ColumnSchema(
                                name=str(column["name"]),
                                dtype=str(column["type"]),
                                nullable=bool(column.get("nullable", True)),
                            )
                            for column in columns
                        ]
                    )
                    qualified = f"{self.schema_name}.{name}" if self.schema_name else name
                    objects.append(
                        DiscoveredObject(
                            name=qualified,
                            kind=kind,  # type: ignore[arg-type]
                            location=qualified,
                            schema=schema,
                        )
                    )
            return objects
        except SQLAlchemyError as exc:
            raise ConnectorError(f"discovery failed: {exc}", cause=exc) from exc

    def infer_schema(self, options: ReadOptions) -> DatasetSchema:
        sql = self._build_query(options, limit=0)
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
        return schema_from_frame(pl.DataFrame({c: [] for c in columns}))

    # ------------------------------------------------------------------ reading
    def read(self, options: ReadOptions) -> Iterator[pl.DataFrame]:
        sql = self._build_query(options)
        chunk_rows = max(options.chunk_rows, 1_000)
        params: dict[str, Any] = {}
        if options.incremental_column and options.since is not None:
            params["since"] = options.since

        emitted = 0
        try:
            with self.engine.connect().execution_options(stream_results=True) as conn:
                result = conn.execute(text(sql), params)
                columns = list(result.keys())
                while True:
                    rows = result.fetchmany(chunk_rows)
                    if not rows:
                        return
                    frame = pl.DataFrame(
                        [tuple(row) for row in rows],
                        schema=columns,
                        orient="row",
                        infer_schema_length=None,
                        strict=False,
                    )
                    if options.limit is not None and emitted + frame.height > options.limit:
                        frame = frame.head(options.limit - emitted)
                    emitted += frame.height
                    yield frame
                    if options.limit is not None and emitted >= options.limit:
                        return
        except SQLAlchemyError as exc:
            raise ConnectorError(
                f"query failed on source '{self.name}': {exc}",
                details={"sql": sql[:500]},
                cause=exc,
            ) from exc

    def _build_query(self, options: ReadOptions, *, limit: int | None = None) -> str:
        """Build a read-only statement. Any caller-supplied SQL passes the safety guard first."""
        raw_query = options.query or self.config.get("query")
        if raw_query:
            statement = guard(str(raw_query), SqlPolicy.read_only(max_rows=0))
            base = f"SELECT * FROM ({statement.sql}) AS _gdap_src"  # noqa: S608 - guarded above
        else:
            table = options.object or self.config.get("table")
            if not table:
                raise ConnectorError(
                    "nothing to read: provide options.object, config['table'] or config['query']"
                )
            qualified = _quote_table(str(table))
            columns = (
                ", ".join(_quote_ident(c) for c in options.columns) if options.columns else "*"
            )
            base = f"SELECT {columns} FROM {qualified}"  # noqa: S608 - identifiers quoted

        clauses: list[str] = []
        if options.incremental_column and options.since is not None:
            clauses.append(f"{_quote_ident(options.incremental_column)} > :since")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = (
            f" ORDER BY {_quote_ident(options.incremental_column)}"
            if options.incremental_column
            else ""
        )
        effective_limit = limit if limit is not None else options.limit
        limit_clause = f" LIMIT {int(effective_limit)}" if effective_limit is not None else ""
        return f"{base}{where}{order}{limit_clause}"


def _quote_ident(name: str) -> str:
    cleaned = name.replace('"', "")
    if not cleaned or not all(ch.isalnum() or ch in "_$" for ch in cleaned):
        raise ConnectorError(f"unsafe identifier: {name!r}")
    return f'"{cleaned}"'


def _quote_table(name: str) -> str:
    return ".".join(_quote_ident(part) for part in name.split("."))


class SqlPlugin(ConnectorPlugin):
    key = "sql"
    source_type = SourceType.SQL
    title = "SQL database"
    description = "PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite and SQLAlchemy dialects."

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": [],
            "properties": {
                "url": {"type": "string", "description": "full SQLAlchemy URL (no credentials)"},
                "driver": {"type": "string", "enum": sorted(_DRIVERS)},
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "database": {"type": "string"},
                "username": {"type": "string"},
                "schema": {"type": "string"},
                "table": {"type": "string"},
                "query": {"type": "string", "description": "read-only SELECT"},
                "connect_args": {"type": "object"},
            },
            "secret_refs": {"password": "env:DB_PASSWORD", "url": "env:DB_URL"},
        }

    def create(self, spec: SourceSpec, secrets: dict[str, str]) -> BaseConnector:
        return SqlConnector(spec, secrets)
