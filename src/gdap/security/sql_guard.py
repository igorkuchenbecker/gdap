"""SQL safety layer (§37).

Every SQL string that reaches an engine passes through here first. Policy is deny-by-default:

======================  =========================================================
SELECT / WITH / EXPLAIN allowed
INSERT / UPDATE         allowed only when the policy *and* the caller's permissions say so
DELETE                  restricted (explicit permission + non-default policy)
DROP / TRUNCATE / ALTER blocked outright
ATTACH / COPY / INSTALL blocked (filesystem & extension escape hatches)
======================  =========================================================

The guard is intentionally conservative and syntax-level: it strips comments and string
literals, rejects statement stacking, and refuses anything it does not positively recognise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from gdap.core.enums import Permission
from gdap.core.errors import SqlSafetyError

StatementKind = Literal[
    "select", "insert", "update", "delete", "create", "drop", "alter", "truncate", "other"
]

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")
_WHITESPACE = re.compile(r"\s+")

#: Statements that let a query touch the filesystem, load extensions or reconfigure the engine.
#: Matched only in leading position (``SET`` inside ``UPDATE … SET`` is a different token).
_ESCAPE_STATEMENTS = {
    "attach",
    "detach",
    "copy",
    "install",
    "load",
    "export",
    "import",
    "pragma",
    "set",
    "reset",
    "call",
}

#: Table/scalar functions that read from disk, the network or the shell. Matched anywhere.
_ESCAPE_FUNCTIONS = {
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_text",
    "read_blob",
    "glob",
    "parquet_scan",
    "csv_scan",
    "sniff_csv",
    "delta_scan",
    "iceberg_scan",
    "postgres_scan",
    "mysql_scan",
    "sqlite_scan",
    "system",
    "shell",
    "getenv",
    "load_extension",
}

#: Clauses (not function calls) that write query results to the filesystem on engines that
#: support them — DuckDB rejects these today, but the guard is meant to hold regardless of which
#: engine ends up executing the statement (see module docstring: "conservative and syntax-level").
_ESCAPE_CLAUSES = re.compile(r"\binto\s+(outfile|dumpfile)\b")

_DESTRUCTIVE = {"drop", "truncate", "alter", "vacuum", "create"}
_WRITE = {"insert", "update", "delete", "merge", "replace", "upsert"}


@dataclass(frozen=True, slots=True)
class SqlPolicy:
    """Secure defaults; every relaxation must be a deliberate, audited decision."""

    allow_select: bool = True
    allow_write: bool = False
    allow_delete: bool = False
    allow_ddl: bool = False
    allow_file_access: bool = False
    max_rows: int = 100_000
    timeout_seconds: int = 30

    @classmethod
    def read_only(cls, *, max_rows: int = 100_000, timeout_seconds: int = 30) -> SqlPolicy:
        return cls(max_rows=max_rows, timeout_seconds=timeout_seconds)

    @classmethod
    def agent(cls) -> SqlPolicy:
        """The strictest policy — what the AI layer gets (§36.6)."""
        return cls(
            allow_write=False,
            allow_delete=False,
            allow_ddl=False,
            allow_file_access=False,
            max_rows=10_000,
            timeout_seconds=20,
        )


@dataclass(slots=True)
class GuardedStatement:
    sql: str
    kind: StatementKind
    read_only: bool
    limit_applied: int | None = None
    referenced: list[str] = field(default_factory=list)

    @property
    def required_permissions(self) -> list[Permission]:
        if self.kind in {"insert", "update"}:
            return [Permission.SQL_WRITE]
        if self.kind in {"delete", "drop", "truncate", "alter", "create"}:
            return [Permission.SQL_DESTRUCTIVE]
        return []


def _normalise(sql: str) -> str:
    stripped = _COMMENT_BLOCK.sub(" ", sql)
    stripped = _COMMENT_LINE.sub(" ", stripped)
    stripped = _STRING_LITERAL.sub("''", stripped)
    return _WHITESPACE.sub(" ", stripped).strip()


def classify(sql: str) -> StatementKind:
    normalised = _normalise(sql).lower()
    if not normalised:
        return "other"
    first = normalised.split(" ", 1)[0].strip("(")
    if first in {"with", "select", "table", "from", "explain", "describe", "show", "values"}:
        # a CTE can still hide a write: WITH x AS (...) INSERT ...
        for keyword in _WRITE | _DESTRUCTIVE:
            if re.search(rf"\b{keyword}\b\s+(into|from|table|view|index)?", normalised):
                return keyword  # type: ignore[return-value]
        return "select"
    if first in _WRITE or first in _DESTRUCTIVE:
        return first  # type: ignore[return-value]
    return "other"


def guard(sql: str, policy: SqlPolicy | None = None) -> GuardedStatement:
    """Validate a statement against a policy, returning a safe-to-execute version."""
    policy = policy or SqlPolicy()
    raw = sql.strip().rstrip(";").strip()
    if not raw:
        raise SqlSafetyError("empty statement")

    normalised = _normalise(raw)
    lowered = normalised.lower()

    if ";" in normalised:
        raise SqlSafetyError(
            "multiple statements are not allowed",
            details={"hint": "submit one statement per request"},
        )

    if not policy.allow_file_access:
        leading = lowered.split(" ", 1)[0].strip("(")
        if leading in _ESCAPE_STATEMENTS:
            raise SqlSafetyError(
                f"'{leading.upper()}' is blocked by the SQL safety layer",
                details={"token": leading, "reason": "filesystem/engine escape hatch"},
            )
        for token in _ESCAPE_FUNCTIONS:
            if re.search(rf"(^|[^a-z0-9_]){token}\s*\(", lowered):
                raise SqlSafetyError(
                    f"'{token}' is blocked by the SQL safety layer",
                    details={"token": token, "reason": "filesystem/network access"},
                )
        if _ESCAPE_CLAUSES.search(lowered):
            raise SqlSafetyError(
                "'INTO OUTFILE/DUMPFILE' is blocked by the SQL safety layer",
                details={"reason": "filesystem escape hatch"},
            )

    kind = classify(raw)

    if kind == "other":
        raise SqlSafetyError(
            "statement type not recognised — only SELECT-style queries are allowed by default",
            details={"statement": normalised[:200]},
        )
    if kind == "select" and not policy.allow_select:
        raise SqlSafetyError("read access is disabled by policy")
    if kind in {"insert", "update"} and not policy.allow_write:
        raise SqlSafetyError(
            f"{kind.upper()} is disabled by policy",
            details={"required": Permission.SQL_WRITE.value},
        )
    if kind == "delete" and not policy.allow_delete:
        raise SqlSafetyError(
            "DELETE is restricted",
            details={"required": Permission.SQL_DESTRUCTIVE.value},
        )
    if kind in {"drop", "truncate", "alter", "create"} and not policy.allow_ddl:
        raise SqlSafetyError(
            f"{kind.upper()} is blocked",
            details={"reason": "DDL is never allowed through the query API"},
        )

    guarded_sql = raw
    limit_applied: int | None = None
    if kind == "select" and policy.max_rows > 0 and not re.search(r"\blimit\s+\d+", lowered):
        # the statement is already classified as read-only; the limit is an int from policy
        guarded_sql = f"SELECT * FROM ({raw}) AS _gdap_guarded LIMIT {policy.max_rows}"  # noqa: S608
        limit_applied = policy.max_rows

    return GuardedStatement(
        sql=guarded_sql,
        kind=kind,
        read_only=kind == "select",
        limit_applied=limit_applied,
        referenced=_referenced_tables(lowered),
    )


def _referenced_tables(lowered: str) -> list[str]:
    """Best-effort extraction of referenced relations — used for lineage and audit, not security."""
    found = re.findall(r"\b(?:from|join|into|update)\s+([a-z_][a-z0-9_.\"]*)", lowered)
    return sorted({name.strip('"') for name in found})
