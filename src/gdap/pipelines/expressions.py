"""Safe expression language for pipeline steps.

Pipelines contain user-authored expressions (``quantity * unit_price * (1 - discount_pct)``).
Evaluating those with :func:`eval` would hand arbitrary code execution to anyone who can write a
pipeline — including the AI planner. So expressions are parsed with :mod:`ast`, validated against
an allow-list of node types and functions, and compiled into **Polars expressions**: no Python
callable is ever built, no attribute access is possible, and an unknown name fails loudly.

Supported: column references, literals, arithmetic, comparison, boolean logic, and the whitelisted
functions in :data:`FUNCTIONS`.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import Any

import polars as pl

from gdap.core.errors import ValidationFailedError

MAX_LENGTH = 2_000
MAX_DEPTH = 25

#: ``if`` is a Python keyword, so ``if(cond, a, b)`` is rewritten to ``if_else(cond, a, b)``
#: before parsing. This is pure sugar for spec authors — the semantics are identical.
_IF_SUGAR = re.compile(r"\bif\b\s*\(")


def _fn_if(condition: pl.Expr, when_true: Any, when_false: Any) -> pl.Expr:
    return pl.when(condition).then(_literal(when_true)).otherwise(_literal(when_false))


def _fn_coalesce(*values: Any) -> pl.Expr:
    return pl.coalesce([_literal(value) for value in values])


def _fn_date_part(expression: pl.Expr, part: str) -> pl.Expr:
    accessor = {
        "year": lambda e: e.dt.year(),
        "quarter": lambda e: e.dt.quarter(),
        "month": lambda e: e.dt.month(),
        "week": lambda e: e.dt.week(),
        "day": lambda e: e.dt.day(),
        "weekday": lambda e: e.dt.weekday(),
        "hour": lambda e: e.dt.hour(),
        "minute": lambda e: e.dt.minute(),
    }.get(str(part).lower())
    if accessor is None:
        raise ValidationFailedError(
            f"unknown date part '{part}'",
            details={
                "supported": [
                    "year",
                    "quarter",
                    "month",
                    "week",
                    "day",
                    "weekday",
                    "hour",
                    "minute",
                ]
            },
        )
    return accessor(expression)


#: Every function a pipeline author (human or AI) may call. Adding one is a deliberate act.
FUNCTIONS: dict[str, Callable[..., Any]] = {
    # numeric
    "abs": lambda e: _expr(e).abs(),
    "round": lambda e, digits=0: _expr(e).round(int(digits)),
    "floor": lambda e: _expr(e).floor(),
    "ceil": lambda e: _expr(e).ceil(),
    "sqrt": lambda e: _expr(e).sqrt(),
    "log": lambda e, base=None: _expr(e).log() if base is None else _expr(e).log(float(base)),
    "exp": lambda e: _expr(e).exp(),
    "clip": lambda e, low, high: _expr(e).clip(low, high),
    # aggregation (window over the whole frame)
    "sum": lambda e: _expr(e).sum(),
    "mean": lambda e: _expr(e).mean(),
    "median": lambda e: _expr(e).median(),
    "min": lambda e: _expr(e).min(),
    "max": lambda e: _expr(e).max(),
    "count": lambda e: _expr(e).count(),
    "std": lambda e: _expr(e).std(),
    "cum_sum": lambda e: _expr(e).cum_sum(),
    # null handling
    "coalesce": _fn_coalesce,
    "fill_null": lambda e, value: _expr(e).fill_null(_literal(value)),
    "is_null": lambda e: _expr(e).is_null(),
    "is_not_null": lambda e: _expr(e).is_not_null(),
    # text
    "upper": lambda e: _expr(e).cast(pl.Utf8).str.to_uppercase(),
    "lower": lambda e: _expr(e).cast(pl.Utf8).str.to_lowercase(),
    "trim": lambda e: _expr(e).cast(pl.Utf8).str.strip_chars(),
    "length": lambda e: _expr(e).cast(pl.Utf8).str.len_chars(),
    "contains": lambda e, pattern: _expr(e).cast(pl.Utf8).str.contains(str(pattern), literal=True),
    "starts_with": lambda e, prefix: _expr(e).cast(pl.Utf8).str.starts_with(str(prefix)),
    "concat": lambda *parts: pl.concat_str([_literal(p) for p in parts], separator=""),
    "replace": lambda e, old, new: (
        _expr(e).cast(pl.Utf8).str.replace_all(str(old), str(new), literal=True)
    ),
    # temporal
    "date_part": _fn_date_part,
    "date_trunc": lambda e, unit: _expr(e).dt.truncate(str(unit)),
    # casting
    "to_number": lambda e: _expr(e).cast(pl.Float64, strict=False),
    "to_int": lambda e: _expr(e).cast(pl.Int64, strict=False),
    "to_text": lambda e: _expr(e).cast(pl.Utf8, strict=False),
    "to_date": lambda e, fmt=None: (
        _expr(e).str.to_date(fmt, strict=False) if fmt else _expr(e).cast(pl.Date, strict=False)
    ),
    # logic
    "if": _fn_if,
    "if_else": _fn_if,
}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.List,
    ast.Tuple,
    ast.keyword,
)


def parse_expression(
    source: str,
    *,
    columns: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> pl.Expr:
    """Compile a text expression into a Polars expression, or fail with a precise error."""
    if not isinstance(source, str) or not source.strip():
        raise ValidationFailedError("expression must be a non-empty string")
    if len(source) > MAX_LENGTH:
        raise ValidationFailedError(f"expression exceeds {MAX_LENGTH} characters")

    prepared = _IF_SUGAR.sub("if_else(", source)
    try:
        tree = ast.parse(prepared, mode="eval")
    except SyntaxError as exc:
        raise ValidationFailedError(
            f"invalid expression syntax: {exc.msg}", details={"expression": source}
        ) from exc

    _validate(tree, source)
    return _compile(tree.body, columns=columns, params=params or {}, depth=0)


def validate_expression(source: str, *, columns: list[str] | None = None) -> bool:
    """Return True when the expression parses and only references known columns."""
    parse_expression(source, columns=columns)
    return True


# ─────────────────────────────────────────── internals ─────────────────────────────────────


def _validate(tree: ast.AST, source: str) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValidationFailedError(
                f"expression construct '{type(node).__name__}' is not allowed",
                details={
                    "expression": source,
                    "reason": "only columns, literals, operators and whitelisted functions are permitted",
                },
            )
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ValidationFailedError(
                "only direct calls to whitelisted functions are allowed",
                details={"expression": source},
            )


def _compile(
    node: ast.AST, *, columns: list[str] | None, params: dict[str, Any], depth: int
) -> Any:
    if depth > MAX_DEPTH:
        raise ValidationFailedError("expression nests too deeply")

    def recurse(child: ast.AST) -> Any:
        return _compile(child, columns=columns, params=params, depth=depth + 1)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        name = node.id
        if name in params:
            return params[name]
        if name.lower() in {"true", "false", "null", "none"}:
            return {"true": True, "false": False, "null": None, "none": None}[name.lower()]
        if columns is not None and name not in columns:
            raise ValidationFailedError(
                f"unknown column '{name}'",
                details={"available_columns": columns, "hint": "params are referenced by name too"},
            )
        return pl.col(name)

    if isinstance(node, ast.List | ast.Tuple):
        return [recurse(element) for element in node.elts]

    if isinstance(node, ast.UnaryOp):
        operand = recurse(node.operand)
        if isinstance(node.op, ast.USub):
            return -_expr(operand) if isinstance(operand, pl.Expr) else -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        return ~_expr(operand)  # ast.Not

    if isinstance(node, ast.BinOp):
        left, right = recurse(node.left), recurse(node.right)
        operations = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b,
            ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a**b,
        }
        return operations[type(node.op)](_operand(left), _operand(right))

    if isinstance(node, ast.BoolOp):
        values = [_expr(recurse(value)) for value in node.values]
        result = values[0]
        for value in values[1:]:
            result = (result & value) if isinstance(node.op, ast.And) else (result | value)
        return result

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValidationFailedError("chained comparisons are not supported")
        left, right = recurse(node.left), recurse(node.comparators[0])
        operator = node.ops[0]
        if isinstance(operator, ast.In):
            return _expr(left).is_in(right)
        if isinstance(operator, ast.NotIn):
            return ~_expr(left).is_in(right)
        comparisons = {
            ast.Eq: lambda a, b: a == b,
            ast.NotEq: lambda a, b: a != b,
            ast.Lt: lambda a, b: a < b,
            ast.LtE: lambda a, b: a <= b,
            ast.Gt: lambda a, b: a > b,
            ast.GtE: lambda a, b: a >= b,
        }
        return comparisons[type(operator)](_operand(left), _operand(right))

    if isinstance(node, ast.Call):
        # _validate() already rejected any call whose func is not a bare Name
        assert isinstance(node.func, ast.Name)
        name = node.func.id
        function = FUNCTIONS.get(name)
        if function is None:
            raise ValidationFailedError(
                f"function '{name}' is not available in pipeline expressions",
                details={"available": sorted(FUNCTIONS)},
            )
        args = [recurse(argument) for argument in node.args]
        kwargs = {kw.arg: recurse(kw.value) for kw in node.keywords if kw.arg}
        try:
            return function(*args, **kwargs)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(
                f"invalid call to '{name}': {exc}", details={"function": name}
            ) from exc

    raise ValidationFailedError(f"unsupported expression element: {type(node).__name__}")


def _expr(value: Any) -> pl.Expr:
    return value if isinstance(value, pl.Expr) else pl.lit(value)


def _literal(value: Any) -> Any:
    return value if isinstance(value, pl.Expr) else pl.lit(value)


def _operand(value: Any) -> Any:
    """Keep literal-only arithmetic in Python; promote to an expression when a column appears."""
    return value


def evaluate_scalar(source: str, variables: dict[str, Any]) -> Any:
    """Evaluate a *scalar* expression (a step's ``when:`` guard) over plain values.

    Same allow-list discipline as :func:`parse_expression`, but the names resolve to run
    parameters and metrics instead of columns, and the result is a Python value.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValidationFailedError("condition must be a non-empty string")
    try:
        tree = ast.parse(_IF_SUGAR.sub("if_else(", source), mode="eval")
    except SyntaxError as exc:
        raise ValidationFailedError(
            f"invalid condition syntax: {exc.msg}", details={"condition": source}
        ) from exc
    _validate(tree, source)
    return _scalar(tree.body, variables, 0)


_SCALAR_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "len": len,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


def _scalar(node: ast.AST, variables: dict[str, Any], depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise ValidationFailedError("condition nests too deeply")

    def recurse(child: ast.AST) -> Any:
        return _scalar(child, variables, depth + 1)

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered in {"true", "false", "null", "none"}:
            return {"true": True, "false": False, "null": None, "none": None}[lowered]
        if node.id not in variables:
            raise ValidationFailedError(
                f"unknown name '{node.id}' in condition",
                details={"available": sorted(variables)[:40]},
            )
        return variables[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [recurse(element) for element in node.elts]
    if isinstance(node, ast.UnaryOp):
        value = recurse(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        return not value
    if isinstance(node, ast.BinOp):
        left, right = recurse(node.left), recurse(node.right)
        operations = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b,
            ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a**b,
        }
        return operations[type(node.op)](left, right)
    if isinstance(node, ast.BoolOp):
        values = [recurse(value) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValidationFailedError("chained comparisons are not supported")
        left, right = recurse(node.left), recurse(node.comparators[0])
        operator = node.ops[0]
        comparisons = {
            ast.Eq: lambda a, b: a == b,
            ast.NotEq: lambda a, b: a != b,
            ast.Lt: lambda a, b: a < b,
            ast.LtE: lambda a, b: a <= b,
            ast.Gt: lambda a, b: a > b,
            ast.GtE: lambda a, b: a >= b,
            ast.In: lambda a, b: a in b,
            ast.NotIn: lambda a, b: a not in b,
        }
        return comparisons[type(operator)](left, right)
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        name = node.func.id
        function = _SCALAR_FUNCTIONS.get(name)
        if function is None:
            raise ValidationFailedError(
                f"function '{name}' is not available in conditions",
                details={"available": sorted(_SCALAR_FUNCTIONS)},
            )
        return function(*[recurse(argument) for argument in node.args])
    raise ValidationFailedError(f"unsupported condition element: {type(node).__name__}")
