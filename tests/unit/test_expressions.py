"""Pipeline expressions must be expressive for authors and inert for attackers."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from gdap.core.errors import ValidationFailedError
from gdap.pipelines.expressions import evaluate_scalar, parse_expression


@pytest.fixture
def frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "quantity": [2, 3, 10],
            "unit_price": [10.0, 20.0, 5.0],
            "discount": [0.0, 0.1, 0.5],
            "region": ["North", "south ", None],
            "day": [dt.date(2026, 1, 15)] * 3,
        }
    )


def _values(frame: pl.DataFrame, expression: str) -> list:
    return frame.select(parse_expression(expression, columns=frame.columns).alias("r"))[
        "r"
    ].to_list()


def test_arithmetic(frame: pl.DataFrame) -> None:
    assert _values(frame, "quantity * unit_price * (1 - discount)") == [20.0, 54.0, 25.0]


def test_text_functions(frame: pl.DataFrame) -> None:
    assert _values(frame, "upper(trim(region))") == ["NORTH", "SOUTH", None]
    assert _values(frame, "coalesce(region, 'unknown')") == ["North", "south ", "unknown"]


def test_conditionals_and_dates(frame: pl.DataFrame) -> None:
    assert _values(frame, "if(quantity > 5, 'bulk', 'standard')") == [
        "standard",
        "standard",
        "bulk",
    ]
    assert _values(frame, "date_part(day, 'month')") == [1, 1, 1]


def test_membership_and_boolean_logic(frame: pl.DataFrame) -> None:
    assert _values(frame, "region in ['North', 'East']") == [True, False, None]
    assert _values(frame, "quantity > 2 and discount < 0.4") == [False, True, False]


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "quantity.__class__.__mro__",
        "[x for x in range(10)]",
        "eval('1+1')",
        "exec('x=1')",
        "globals()",
        "(lambda: 1)()",
    ],
)
def test_code_execution_is_impossible(frame: pl.DataFrame, expression: str) -> None:
    with pytest.raises(ValidationFailedError):
        parse_expression(expression, columns=frame.columns)


def test_unknown_column_fails_loudly(frame: pl.DataFrame) -> None:
    with pytest.raises(ValidationFailedError, match="unknown column"):
        parse_expression("nonexistent + 1", columns=frame.columns)


def test_unknown_function_fails_loudly(frame: pl.DataFrame) -> None:
    with pytest.raises(ValidationFailedError, match="not available"):
        parse_expression("mystery(quantity)", columns=frame.columns)


def test_scalar_conditions() -> None:
    variables = {"quality_score": 91.4, "rows": 3416, "env": "prod"}
    assert evaluate_scalar("quality_score < 95", variables) is True
    assert evaluate_scalar("rows > 1000 and env == 'prod'", variables) is True
    assert evaluate_scalar("env in ['dev', 'staging']", variables) is False


def test_scalar_condition_rejects_unknown_names() -> None:
    with pytest.raises(ValidationFailedError, match="unknown name"):
        evaluate_scalar("mystery > 1", {})
