"""Analytical query engine (DuckDB adapter for the :class:`QueryEngine` port).

DuckDB queries the Parquet files in the warehouse directly — vectorised, out-of-core, no data
copy. Datasets are exposed as *views*; user-supplied SQL is validated by
:mod:`gdap.security.sql_guard` before it ever reaches the engine, and every statement runs under a
wall-clock timeout enforced by ``interrupt()``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from gdap.core.errors import GdapError, StorageError, ValidationFailedError
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.security.sql_guard import GuardedStatement, SqlPolicy, guard

log = get_logger(__name__)


class QueryTimeoutError(GdapError):
    code = "GDAP-4201"
    http_status = 504
    message = "Query exceeded the configured time budget"


def _quote_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    if not name.replace("_", "").replace(".", "").isalnum():
        raise ValidationFailedError(f"invalid relation name: {name!r}")
    return '"' + name.replace('"', '""') + '"'


class DuckDBEngine:
    """One engine per unit of work (a request, a job). Cheap to build, safe to discard."""

    def __init__(
        self,
        *,
        threads: int | None = None,
        memory_limit: str | None = None,
        default_policy: SqlPolicy | None = None,
    ) -> None:
        self._con = duckdb.connect(database=":memory:")
        self._policy = default_policy or SqlPolicy.read_only()
        self._registered: dict[str, str] = {}
        if threads:
            self._con.execute(f"SET threads TO {int(threads)}")
        if memory_limit:
            self._con.execute(f"SET memory_limit = '{memory_limit}'")
        self._con.execute("SET enable_progress_bar = false")

    # ------------------------------------------------------------------ registration
    def register_parquet(self, name: str, path: Path | str) -> None:
        """Expose a warehouse file as a view. Internal call — not reachable from user SQL."""
        file_path = Path(str(path).replace("file://", ""))
        if not file_path.exists():
            raise StorageError(f"cannot register '{name}': {file_path} does not exist")
        # internal DDL: the view name and the file path are quoted by the helpers above
        self._con.execute(
            f"CREATE OR REPLACE VIEW {_quote_ident(name)} AS "  # noqa: S608
            f"SELECT * FROM read_parquet({_quote_path(file_path)})"
        )
        self._registered[name] = str(file_path)

    def register(self, name: str, source: Any) -> None:
        """Register an in-memory frame (Polars/Arrow) as a queryable relation."""
        if isinstance(source, pl.DataFrame):
            self._con.register(name, source.to_arrow())
        elif isinstance(source, pl.LazyFrame):
            self._con.register(name, source.collect().to_arrow())
        elif isinstance(source, str | Path):
            self.register_parquet(name, source)
            return
        else:
            self._con.register(name, source)
        self._registered[name] = "<in-memory>"

    @property
    def relations(self) -> dict[str, str]:
        return dict(self._registered)

    # ------------------------------------------------------------------ execution
    def query(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        policy: SqlPolicy | None = None,
        limit: int | None = None,
    ) -> pl.DataFrame:
        """Guard, execute and materialise a statement. Returns an empty frame for writes."""
        effective = policy or self._policy
        if limit is not None:
            effective = SqlPolicy(
                allow_select=effective.allow_select,
                allow_write=effective.allow_write,
                allow_delete=effective.allow_delete,
                allow_ddl=effective.allow_ddl,
                allow_file_access=effective.allow_file_access,
                max_rows=limit,
                timeout_seconds=effective.timeout_seconds,
            )
        statement = guard(sql, effective)
        return self.execute_guarded(statement, params=params, timeout=effective.timeout_seconds)

    def execute_guarded(
        self,
        statement: GuardedStatement,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> pl.DataFrame:
        timer = threading.Timer(timeout, self._con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            with METRICS.timer("sql_query_ms", kind=statement.kind):
                cursor = (
                    self._con.execute(statement.sql, params)
                    if params
                    else self._con.execute(statement.sql)
                )
                result = cursor.pl() if cursor.description else pl.DataFrame()
        except duckdb.InterruptException as exc:
            raise QueryTimeoutError(
                f"query cancelled after {timeout}s", details={"sql": statement.sql[:500]}
            ) from exc
        except duckdb.Error as exc:
            METRICS.increment("sql_query_errors")
            raise ValidationFailedError(
                f"SQL error: {exc}", details={"sql": statement.sql[:500]}
            ) from exc
        finally:
            timer.cancel()

        METRICS.increment("sql_queries_total", kind=statement.kind)
        log.debug(
            "sql_executed",
            kind=statement.kind,
            rows=result.height,
            relations=statement.referenced,
        )
        return result

    def explain(self, sql: str, *, policy: SqlPolicy | None = None) -> str:
        statement = guard(sql, policy or self._policy)
        rows = self._con.execute(f"EXPLAIN {statement.sql}").fetchall()
        return "\n".join(str(row[-1]) for row in rows)

    def sql_unguarded(self, sql: str) -> pl.DataFrame:
        """Internal, trusted SQL built by the platform itself (never user input)."""
        cursor = self._con.execute(sql)
        return cursor.pl() if cursor.description else pl.DataFrame()

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> DuckDBEngine:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
