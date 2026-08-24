"""Transformation steps: calculate, select, filter, rename, cast, sort, aggregate, join, enrich."""

from __future__ import annotations

from typing import Any

import polars as pl

from gdap.core.contracts import StepSpec
from gdap.core.errors import ValidationFailedError
from gdap.core.frames import require_columns
from gdap.pipelines.expressions import parse_expression
from gdap.pipelines.steps.registry import StepContext, StepOutcome, register_step

_AGGREGATIONS = {
    "sum": lambda column: pl.col(column).sum(),
    "mean": lambda column: pl.col(column).mean(),
    "avg": lambda column: pl.col(column).mean(),
    "median": lambda column: pl.col(column).median(),
    "min": lambda column: pl.col(column).min(),
    "max": lambda column: pl.col(column).max(),
    "count": lambda column: pl.col(column).count(),
    "n_unique": lambda column: pl.col(column).n_unique(),
    "std": lambda column: pl.col(column).std(),
    "first": lambda column: pl.col(column).first(),
    "last": lambda column: pl.col(column).last(),
}


@register_step(
    "transform.calculate",
    description="Add or replace columns from safe expressions (no Python eval).",
    category="transform",
    options={"calculate": "mapping of new_column -> expression"},
)
def calculate(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    definitions: dict[str, str] = context.option(step, "calculate", required=True)
    if not isinstance(definitions, dict):
        raise ValidationFailedError("'calculate' must be a mapping of column -> expression")

    expressions = []
    for name, expression in definitions.items():
        available = frame.columns + [n for n in definitions if n != name]
        expressions.append(
            parse_expression(str(expression), columns=available, params=context.params).alias(name)
        )
    result = frame.with_columns(expressions)
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"calculated {len(definitions)} column(s): {', '.join(definitions)}",
        metrics={"columns_added": len(definitions)},
    )


@register_step(
    "transform.select",
    description="Keep (or drop) a subset of columns.",
    category="transform",
    options={"columns": "columns to keep", "drop": "columns to drop"},
)
def select(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    keep = context.option(step, "columns")
    drop = context.option(step, "drop")
    if keep:
        require_columns(frame, list(keep), context="transform.select")
        result = frame.select(list(keep))
    elif drop:
        result = frame.drop([c for c in drop if c in frame.columns])
    else:
        raise ValidationFailedError("transform.select needs 'columns' or 'drop'")
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"{result.width} column(s) kept",
        metrics={"columns": result.width},
    )


@register_step(
    "transform.filter",
    description="Keep rows matching a safe boolean expression.",
    category="transform",
    options={"where": "boolean expression, e.g. status == 'completed' and revenue > 0"},
)
def filter_rows(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    condition = str(context.option(step, "where", required=True))
    expression = parse_expression(condition, columns=frame.columns, params=context.params)
    result = frame.filter(expression)
    removed = frame.height - result.height
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"kept {result.height:,} of {frame.height:,} rows ({removed:,} filtered out)",
        metrics={"rows_in": frame.height, "rows_out": result.height, "rows_removed": removed},
    )


@register_step(
    "transform.rename",
    description="Rename columns.",
    category="transform",
    options={"rename": "mapping of old_name -> new_name"},
)
def rename(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    mapping: dict[str, str] = context.option(step, "rename", required=True)
    unknown = [name for name in mapping if name not in frame.columns]
    if unknown:
        raise ValidationFailedError(
            f"cannot rename missing column(s): {', '.join(unknown)}",
            details={"available": frame.columns},
        )
    result = frame.rename(mapping)
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(frame=result, message=f"renamed {len(mapping)} column(s)")


@register_step(
    "transform.cast",
    description="Cast columns to explicit types (non-strict: unparseable values become null).",
    category="transform",
    options={"cast": "mapping of column -> int|float|str|bool|date|datetime"},
)
def cast(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    mapping: dict[str, str] = context.option(step, "cast", required=True)
    types = {
        "int": pl.Int64,
        "integer": pl.Int64,
        "float": pl.Float64,
        "number": pl.Float64,
        "str": pl.Utf8,
        "string": pl.Utf8,
        "text": pl.Utf8,
        "bool": pl.Boolean,
        "boolean": pl.Boolean,
        "date": pl.Date,
        "datetime": pl.Datetime,
    }
    expressions = []
    for column, target in mapping.items():
        dtype = types.get(str(target).lower())
        if dtype is None:
            raise ValidationFailedError(
                f"unknown target type '{target}'", details={"supported": sorted(types)}
            )
        require_columns(frame, [column], context="transform.cast")
        expressions.append(pl.col(column).cast(dtype, strict=False).alias(column))
    result = frame.with_columns(expressions)
    nulls_created = sum(
        int(result[column].null_count() - frame[column].null_count()) for column in mapping
    )
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"cast {len(mapping)} column(s)"
        + (f"; {nulls_created} value(s) could not be parsed" if nulls_created else ""),
        metrics={"cast_columns": len(mapping), "unparseable_values": nulls_created},
    )


@register_step(
    "transform.sort",
    description="Sort rows.",
    category="transform",
    options={"by": "column or list of columns", "descending": "bool or list of bools"},
)
def sort(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    by = context.option(step, "by", required=True)
    columns = [by] if isinstance(by, str) else list(by)
    require_columns(frame, columns, context="transform.sort")
    result = frame.sort(columns, descending=context.option(step, "descending", False))
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(frame=result, message=f"sorted by {', '.join(columns)}")


@register_step(
    "transform.deduplicate",
    description="Remove duplicate rows, optionally by a subset of key columns.",
    read_only=False,
    category="transform",
    options={"subset": "key columns", "keep": "first|last"},
)
def deduplicate(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    subset = context.option(step, "subset")
    if subset:
        require_columns(frame, list(subset), context="transform.deduplicate")
    result = frame.unique(
        subset=list(subset) if subset else None,
        keep=str(context.option(step, "keep", "first")),  # type: ignore[arg-type]
        maintain_order=True,
    )
    removed = frame.height - result.height
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"removed {removed:,} duplicate row(s)",
        metrics={"duplicates_removed": removed, "rows_out": result.height},
    )


@register_step(
    "aggregate",
    description="Group rows and compute metrics.",
    category="transform",
    options={
        "group_by": "column or list of columns",
        "metrics": "mapping of output -> 'agg(column)' or {agg, column}",
        "having": "optional filter over the aggregated frame",
    },
)
def aggregate(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    group_by = context.option(step, "group_by", required=True)
    keys = [group_by] if isinstance(group_by, str) else list(group_by)
    require_columns(frame, keys, context="aggregate")

    metrics: dict[str, Any] = context.option(step, "metrics", required=True)
    expressions = []
    for output_name, definition in metrics.items():
        aggregation, column = _parse_metric(definition, output_name)
        if column != "*":
            require_columns(frame, [column], context="aggregate")
        builder = _AGGREGATIONS.get(aggregation)
        if builder is None:
            raise ValidationFailedError(
                f"unsupported aggregation '{aggregation}'",
                details={"supported": sorted(_AGGREGATIONS)},
            )
        expressions.append((pl.len() if column == "*" else builder(column)).alias(output_name))

    result = frame.group_by(keys).agg(expressions).sort(keys)
    having = context.option(step, "having")
    if having:
        result = result.filter(
            parse_expression(str(having), columns=result.columns, params=context.params)
        )
    context.publish(step.output or "aggregated", result)
    return StepOutcome(
        frame=result,
        message=f"aggregated into {result.height:,} group(s) by {', '.join(keys)}",
        metrics={"groups": result.height, "group_by": keys, "metrics": list(metrics)},
    )


@register_step(
    "join",
    description="Join the working frame with another frame or dataset.",
    category="transform",
    options={
        "with_dataset": "dataset to join against",
        "with_frame": "name of a frame produced earlier in the pipeline",
        "on": "join key(s)",
        "left_on": "left key(s)",
        "right_on": "right key(s)",
        "how": "left|inner|outer|semi|anti (default left)",
    },
)
def join(context: StepContext, step: StepSpec) -> StepOutcome:
    left = context.frame(step.input)
    dataset = context.option(step, "with_dataset")
    frame_name = context.option(step, "with_frame")
    if dataset:
        right = context.services.datasets.frame(str(dataset))
        right_label = str(dataset)
    elif frame_name:
        right = context.frame(str(frame_name))
        right_label = str(frame_name)
    else:
        raise ValidationFailedError("join requires 'with_dataset' or 'with_frame'")

    how = str(context.option(step, "how", "left"))
    on = context.option(step, "on")
    left_on = context.option(step, "left_on")
    right_on = context.option(step, "right_on")

    if on:
        keys = [on] if isinstance(on, str) else list(on)
        require_columns(left, keys, context="join (left)")
        require_columns(right, keys, context="join (right)")
        result = left.join(right, on=keys, how=how, coalesce=True)  # type: ignore[arg-type]
    elif left_on and right_on:
        result = left.join(
            right,
            left_on=[left_on] if isinstance(left_on, str) else list(left_on),
            right_on=[right_on] if isinstance(right_on, str) else list(right_on),
            how=how,  # type: ignore[arg-type]
            coalesce=True,
        )
    else:
        raise ValidationFailedError("join requires 'on' or both 'left_on' and 'right_on'")

    unmatched = (
        left.height - result.height
        if how in {"inner", "semi"}
        else result[result.columns[-1]].null_count()
        if how == "left" and result.width > left.width
        else 0
    )
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"{how} join with '{right_label}' → {result.height:,} rows",
        metrics={
            "rows_in": left.height,
            "rows_out": result.height,
            "join_type": how,
            "unmatched_rows": int(unmatched),
        },
    )


@register_step(
    "enrich.datetime",
    description="Derive calendar parts (year, quarter, month, week, weekday) from a date column.",
    category="transform",
    options={"column": "temporal column", "parts": "list of year|quarter|month|week|day|weekday"},
)
def enrich_datetime(context: StepContext, step: StepSpec) -> StepOutcome:
    frame = context.frame(step.input)
    column = str(context.option(step, "column", required=True))
    require_columns(frame, [column], context="enrich.datetime")
    parts = list(context.option(step, "parts", ["year", "quarter", "month", "week", "weekday"]))

    accessors = {
        "year": lambda expression: expression.dt.year(),
        "quarter": lambda expression: expression.dt.quarter(),
        "month": lambda expression: expression.dt.month(),
        "week": lambda expression: expression.dt.week(),
        "day": lambda expression: expression.dt.day(),
        "weekday": lambda expression: expression.dt.weekday(),
        "hour": lambda expression: expression.dt.hour(),
    }
    expressions = []
    for part in parts:
        accessor = accessors.get(str(part).lower())
        if accessor is None:
            raise ValidationFailedError(
                f"unknown datetime part '{part}'", details={"supported": sorted(accessors)}
            )
        expressions.append(accessor(pl.col(column)).alias(f"{column}_{part}"))
    result = frame.with_columns(expressions)
    context.publish(step.output or context.current or "data", result)
    return StepOutcome(
        frame=result,
        message=f"derived {len(parts)} calendar part(s) from '{column}'",
        metrics={"parts": parts},
    )


def _parse_metric(definition: Any, output_name: str) -> tuple[str, str]:
    """Accept ``sum(revenue)``, ``{"agg": "sum", "column": "revenue"}`` or ``count``."""
    if isinstance(definition, dict):
        return str(definition.get("agg", "sum")).lower(), str(definition.get("column", "*"))
    text = str(definition).strip()
    if "(" in text and text.endswith(")"):
        aggregation, _, column = text.partition("(")
        return aggregation.strip().lower(), column[:-1].strip() or "*"
    if text.lower() in {"count", "rows"}:
        return "count", "*"
    return text.lower(), output_name
