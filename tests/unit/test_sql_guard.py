"""The SQL safety layer is a security boundary — it gets adversarial tests."""

from __future__ import annotations

import pytest

from gdap.core.enums import Permission
from gdap.core.errors import SqlSafetyError
from gdap.security.sql_guard import SqlPolicy, guard


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE sales",
        "TRUNCATE sales",
        "ALTER TABLE sales ADD COLUMN x INT",
        "CREATE TABLE evil AS SELECT 1",
        "DELETE FROM sales",
        "UPDATE sales SET revenue = 0",
        "INSERT INTO sales VALUES (1)",
        "WITH x AS (SELECT 1) INSERT INTO sales SELECT * FROM x",
        "SELECT 1; DROP TABLE sales",
        "COPY sales TO '/tmp/exfiltrated.csv'",
        "ATTACH '/etc/shadow' AS leak",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET memory_limit='999GB'",
        "PRAGMA database_list",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('/root/secrets.parquet')",
        "SELECT getenv('ANTHROPIC_API_KEY')",
        "SELECT * FROM glob('/**')",
    ],
)
def test_dangerous_statements_are_blocked_by_default(statement: str) -> None:
    with pytest.raises(SqlSafetyError):
        guard(statement)


def test_select_is_allowed_and_capped() -> None:
    result = guard("SELECT region, sum(revenue) FROM sales GROUP BY region")
    assert result.kind == "select"
    assert result.read_only
    assert result.limit_applied == SqlPolicy().max_rows
    assert "sales" in result.referenced


def test_existing_limit_is_respected() -> None:
    assert guard("SELECT * FROM sales LIMIT 10").limit_applied is None


def test_write_requires_both_policy_and_permission() -> None:
    with pytest.raises(SqlSafetyError):
        guard("UPDATE sales SET revenue = 1")

    allowed = guard("UPDATE sales SET revenue = 1", SqlPolicy(allow_write=True))
    assert allowed.kind == "update"
    assert Permission.SQL_WRITE in allowed.required_permissions


def test_delete_requires_the_destructive_policy() -> None:
    with pytest.raises(SqlSafetyError):
        guard("DELETE FROM sales WHERE 1=1", SqlPolicy(allow_write=True))
    assert guard("DELETE FROM sales", SqlPolicy(allow_delete=True)).kind == "delete"


def test_agent_policy_is_the_strictest() -> None:
    policy = SqlPolicy.agent()
    assert not policy.allow_write
    assert not policy.allow_file_access
    assert policy.max_rows <= 10_000


def test_comments_cannot_hide_a_write() -> None:
    with pytest.raises(SqlSafetyError):
        guard("/* harmless */ DELETE FROM sales -- cleanup")


def test_empty_statement_is_rejected() -> None:
    with pytest.raises(SqlSafetyError):
        guard("   ")
