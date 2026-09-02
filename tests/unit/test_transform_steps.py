"""Transform steps, called directly — select/rename/cast/sort/join/enrich had no coverage at
all beyond calculate/filter/deduplicate exercised incidentally through pipeline tests."""

from __future__ import annotations

import polars as pl
import pytest

from gdap.core.contracts import StepSpec
from gdap.core.errors import ValidationFailedError
from gdap.pipelines.steps.registry import StepContext
from gdap.pipelines.steps.transform import cast, enrich_datetime, join, rename, select, sort


def test_select_keeps_only_the_requested_columns() -> None:
    frame = pl.DataFrame({"a": [1, 2], "b": [3, 4], "c": [5, 6]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.select", input="data", options={"columns": ["a", "c"]})

    outcome = select(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame.columns == ["a", "c"]


def test_select_drops_the_requested_columns() -> None:
    frame = pl.DataFrame({"a": [1], "b": [2], "c": [3]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.select", input="data", options={"drop": ["b"]})

    outcome = select(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame.columns == ["a", "c"]


def test_select_without_columns_or_drop_is_rejected() -> None:
    frame = pl.DataFrame({"a": [1]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.select", input="data", options={})

    with pytest.raises(ValidationFailedError, match="needs 'columns' or 'drop'"):
        select(step_context, step)


def test_rename_maps_old_names_to_new_ones() -> None:
    frame = pl.DataFrame({"old": [1, 2]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.rename", input="data", options={"rename": {"old": "new"}})

    outcome = rename(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame.columns == ["new"]


def test_rename_rejects_a_column_that_does_not_exist() -> None:
    frame = pl.DataFrame({"a": [1]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.rename", input="data", options={"rename": {"missing": "x"}})

    with pytest.raises(ValidationFailedError, match="missing"):
        rename(step_context, step)


def test_cast_converts_types_and_reports_unparseable_values() -> None:
    frame = pl.DataFrame({"n": ["1", "2", "not_a_number"]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.cast", input="data", options={"cast": {"n": "int"}})

    outcome = cast(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame["n"].dtype == pl.Int64
    assert outcome.metrics["unparseable_values"] == 1
    assert "1 value(s) could not be parsed" in outcome.message


def test_cast_rejects_an_unknown_target_type() -> None:
    frame = pl.DataFrame({"n": ["1"]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.cast", input="data", options={"cast": {"n": "bogus_type"}})

    with pytest.raises(ValidationFailedError, match="unknown target type"):
        cast(step_context, step)


def test_sort_orders_rows_by_the_given_column() -> None:
    frame = pl.DataFrame({"a": [3, 1, 2]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("transform.sort", input="data", options={"by": "a"})

    outcome = sort(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame["a"].to_list() == [1, 2, 3]


def test_enrich_datetime_derives_requested_calendar_parts() -> None:
    import datetime as dt

    frame = pl.DataFrame({"order_date": [dt.date(2026, 3, 15)]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of(
        "enrich.datetime",
        input="data",
        options={"column": "order_date", "parts": ["year", "month"]},
    )

    outcome = enrich_datetime(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame["order_date_year"].to_list() == [2026]
    assert outcome.frame["order_date_month"].to_list() == [3]
    assert "order_date_week" not in outcome.frame.columns


def test_enrich_datetime_rejects_an_unknown_part() -> None:
    import datetime as dt

    frame = pl.DataFrame({"d": [dt.date(2026, 1, 1)]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of(
        "enrich.datetime", input="data", options={"column": "d", "parts": ["century"]}
    )

    with pytest.raises(ValidationFailedError, match="unknown datetime part"):
        enrich_datetime(step_context, step)


def test_left_join_unmatched_count_ignores_legitimate_nulls_on_matched_rows() -> None:
    """Regression: counting nulls in the trailing column over-counted 'unmatched' rows whenever
    a genuinely matched row had a real null there — an anti-join is the only reliable count."""
    left = pl.DataFrame({"id": [1, 2, 3, 4], "name": ["a", "b", "c", "d"]})
    right = pl.DataFrame({"id": [1, 2], "x": [10, 20], "y": [100, None]})
    step_context = StepContext(
        services=None,  # type: ignore[arg-type]
        params={},
        frames={"data": left, "right": right},
        current="data",
    )
    step = StepSpec.of(
        "join", input="data", options={"with_frame": "right", "on": "id", "how": "left"}
    )

    outcome = join(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame.height == 4
    # ids 3 and 4 truly have no match on the right; id 2's null 'y' is a real value, not a miss.
    assert outcome.metrics["unmatched_rows"] == 2


def test_inner_join_unmatched_count_is_rows_dropped() -> None:
    left = pl.DataFrame({"id": [1, 2, 3]})
    right = pl.DataFrame({"id": [1, 2], "x": [10, 20]})
    step_context = StepContext(
        services=None,  # type: ignore[arg-type]
        params={},
        frames={"data": left, "right": right},
        current="data",
    )
    step = StepSpec.of(
        "join", input="data", options={"with_frame": "right", "on": "id", "how": "inner"}
    )

    outcome = join(step_context, step)

    assert outcome.frame is not None
    assert outcome.frame.height == 2
    assert outcome.metrics["unmatched_rows"] == 1


def test_join_requires_a_target() -> None:
    frame = pl.DataFrame({"id": [1]})
    step_context = StepContext(services=None, params={}, frames={"data": frame}, current="data")  # type: ignore[arg-type]
    step = StepSpec.of("join", input="data", options={"on": "id"})

    with pytest.raises(ValidationFailedError, match="with_dataset' or 'with_frame'"):
        join(step_context, step)
