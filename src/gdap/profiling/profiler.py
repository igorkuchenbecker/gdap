"""Data profiler (§7).

Produces a :class:`DatasetProfile`: schema, per-column statistics, duplicates, candidate keys,
correlations, semantic metadata and actionable recommendations. Large datasets are profiled on a
deterministic sample (recorded in the profile) so that profiling cost stays bounded.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl

from gdap.core.contracts import (
    ColumnProfile,
    DatasetProfile,
    NumericStats,
    RelationshipHint,
    TemporalStats,
    TextStats,
)
from gdap.core.enums import SemanticType
from gdap.core.frames import (
    is_numeric,
    is_temporal,
    json_safe,
    schema_from_frame,
    to_float,
    to_int,
)
from gdap.governance.classification import classify_column
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.profiling.semantics import infer_semantic_type

log = get_logger(__name__)

DEFAULT_SAMPLE_ROWS = 500_000
TOP_VALUES = 10
HISTOGRAM_BINS = 20


class DataProfiler:
    def __init__(
        self,
        *,
        sample_rows: int = DEFAULT_SAMPLE_ROWS,
        top_values: int = TOP_VALUES,
        histogram_bins: int = HISTOGRAM_BINS,
        detect_candidate_keys: bool = True,
    ) -> None:
        self.sample_rows = sample_rows
        self.top_values = top_values
        self.histogram_bins = histogram_bins
        self.detect_candidate_keys = detect_candidate_keys

    def profile(
        self,
        frame: pl.DataFrame,
        *,
        dataset: str,
        dataset_version_id: str | None = None,
    ) -> DatasetProfile:
        with METRICS.timer("profile_ms"):
            total_rows = frame.height
            sampled = total_rows > self.sample_rows
            working = frame.sample(n=self.sample_rows, seed=42, shuffle=False) if sampled else frame

            column_profiles = [
                self._profile_column(working[name], rows=working.height) for name in working.columns
            ]

            schema = schema_from_frame(frame)
            enriched_columns = []
            for column in schema.columns:
                measured = next((c for c in column_profiles if c.name == column.name), None)
                updated = column.model_copy(
                    update={
                        "semantic_type": measured.semantic_type
                        if measured
                        else SemanticType.UNKNOWN,
                        "nullable": bool(measured.null_count) if measured else column.nullable,
                    }
                )
                enriched_columns.append(
                    updated.model_copy(update={"classification": classify_column(updated)})
                )
            schema = schema.model_copy(update={"columns": enriched_columns})
            for measured_column in column_profiles:
                match = schema.column(measured_column.name)
                if match:
                    measured_column.classification = match.classification

            duplicates = self._count_duplicates(working)
            candidate_keys = self._candidate_keys(working, column_profiles)
            correlations = self._correlations(working)

            profile = DatasetProfile(
                dataset=dataset,
                dataset_version_id=dataset_version_id,
                rows=total_rows,
                columns=len(working.columns),
                memory_bytes=int(frame.estimated_size()),
                schema=schema,
                column_profiles=column_profiles,
                duplicate_rows=duplicates,
                duplicate_ratio=duplicates / working.height if working.height else 0.0,
                candidate_keys=candidate_keys,
                correlations=correlations,
                sampled=sampled,
                sample_rows=working.height if sampled else None,
            )
            profile.recommendations = self._recommendations(profile)
            log.info(
                "dataset_profiled",
                dataset=dataset,
                rows=total_rows,
                columns=len(working.columns),
                sampled=sampled,
            )
            return profile

    # ------------------------------------------------------------------ columns
    def _profile_column(self, series: pl.Series, *, rows: int) -> ColumnProfile:
        null_count = int(series.null_count())
        non_null = series.drop_nulls()
        distinct = int(non_null.n_unique()) if rows else 0
        distinct_ratio = distinct / max(rows - null_count, 1)
        is_unique = distinct == (rows - null_count) and rows > 0 and null_count < rows

        semantic = infer_semantic_type(series, distinct_ratio=distinct_ratio, is_unique=is_unique)

        profile = ColumnProfile(
            name=series.name,
            dtype=str(series.dtype),
            semantic_type=semantic,
            count=rows,
            null_count=null_count,
            null_ratio=null_count / rows if rows else 0.0,
            distinct_count=distinct,
            distinct_ratio=distinct_ratio,
            is_constant=distinct <= 1 and rows > 0,
            is_unique=is_unique,
            is_candidate_key=is_unique and null_count == 0,
            sample_values=[json_safe(v) for v in non_null.head(5).to_list()],
        )

        if not non_null.is_empty() and semantic not in {
            SemanticType.FREE_TEXT,
            SemanticType.JSON_BLOB,
        }:
            profile.top_values = self._top_values(non_null)

        if is_numeric(series.dtype):
            profile.numeric = self._numeric_stats(non_null)
        elif is_temporal(series.dtype):
            profile.temporal = self._temporal_stats(non_null)
        elif series.dtype in (pl.Utf8, pl.Categorical, pl.Enum):
            profile.text = self._text_stats(non_null)

        return profile

    def _top_values(self, series: pl.Series) -> list[tuple[Any, int]]:
        try:
            counts = series.value_counts(sort=True).head(self.top_values)
            value_column, count_column = counts.columns[0], counts.columns[1]
            return [
                (json_safe(row[value_column]), int(row[count_column]))
                for row in counts.iter_rows(named=True)
            ]
        except Exception:  # pragma: no cover - exotic dtypes
            return []

    def _numeric_stats(self, series: pl.Series) -> NumericStats:
        if series.is_empty():
            return NumericStats()
        values = series.cast(pl.Float64, strict=False).drop_nulls()
        if values.is_empty():
            return NumericStats()
        array = values.to_numpy()
        q1, q3 = float(np.percentile(array, 25)), float(np.percentile(array, 75))
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        outliers = int(((array < lower) | (array > upper)).sum()) if iqr > 0 else 0

        counts, edges = np.histogram(array, bins=min(self.histogram_bins, max(len(array), 1)))
        histogram = [
            (float(edges[i]), float(edges[i + 1]), int(counts[i])) for i in range(len(counts))
        ]

        return NumericStats(
            min=float(array.min()),
            max=float(array.max()),
            mean=float(array.mean()),
            median=float(np.median(array)),
            std=float(array.std(ddof=1)) if len(array) > 1 else 0.0,
            variance=float(array.var(ddof=1)) if len(array) > 1 else 0.0,
            skewness=_safe(values.skew()),
            kurtosis=_safe(values.kurtosis()),
            p01=float(np.percentile(array, 1)),
            p05=float(np.percentile(array, 5)),
            p25=q1,
            p75=q3,
            p95=float(np.percentile(array, 95)),
            p99=float(np.percentile(array, 99)),
            zeros=int((array == 0).sum()),
            negatives=int((array < 0).sum()),
            outlier_count=outliers,
            outlier_bounds=(lower, upper) if iqr > 0 else None,
            histogram=histogram,
        )

    def _temporal_stats(self, series: pl.Series) -> TemporalStats:
        if series.is_empty():
            return TemporalStats()
        minimum, maximum = series.min(), series.max()
        stats = TemporalStats()
        try:
            as_datetime = series.cast(pl.Datetime, strict=False).drop_nulls()
            stats.min = _to_datetime(minimum)
            stats.max = _to_datetime(maximum)
            if stats.min and stats.max:
                stats.span_days = (stats.max - stats.min).total_seconds() / 86400
            if as_datetime.len() > 2:
                deltas = as_datetime.sort().diff().drop_nulls()
                if deltas.len():
                    median_seconds = to_float(deltas.dt.total_seconds().median())
                    stats.inferred_granularity = _granularity(median_seconds)
                    if median_seconds > 0:
                        expected = (
                            (stats.span_days * 86400 / median_seconds) if stats.span_days else 0
                        )
                        stats.gaps = max(int(expected) - as_datetime.n_unique(), 0)
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            threshold = now.replace(tzinfo=None)
            future = as_datetime.filter(as_datetime > threshold)
            stats.future_values = int(future.len())
        except Exception as exc:  # pragma: no cover - exotic temporal types
            log.debug("temporal_stats_partial", column=series.name, error=str(exc))
        return stats

    def _text_stats(self, series: pl.Series) -> TextStats:
        if series.is_empty():
            return TextStats()
        text = series.cast(pl.Utf8, strict=False).drop_nulls()
        lengths = text.str.len_chars()
        stripped = text.str.strip_chars()
        return TextStats(
            min_length=to_int(lengths.min()),
            max_length=to_int(lengths.max()),
            avg_length=to_float(lengths.mean()),
            empty_strings=int((stripped == "").sum()),
            whitespace_issues=int((stripped != text).sum()),
            detected_patterns=_detect_patterns(text),
        )

    # ------------------------------------------------------------------ dataset level
    def _count_duplicates(self, frame: pl.DataFrame) -> int:
        if frame.is_empty():
            return 0
        try:
            hashable = frame.select(
                [
                    pl.col(name).cast(pl.Utf8, strict=False)
                    for name, dtype in frame.schema.items()
                    if not isinstance(dtype, pl.List | pl.Struct)
                ]
            )
            return int(hashable.height - hashable.unique().height)
        except Exception:  # pragma: no cover
            return 0

    def _candidate_keys(self, frame: pl.DataFrame, columns: list[ColumnProfile]) -> list[list[str]]:
        if not self.detect_candidate_keys or frame.is_empty():
            return []
        keys: list[list[str]] = [[c.name] for c in columns if c.is_candidate_key]
        if keys:
            return keys
        # composite keys of two columns, restricted to low-cardinality-safe candidates
        candidates = [
            c.name
            for c in columns
            if not c.is_constant and c.null_count == 0 and c.distinct_ratio > 0.05
        ][:8]
        for i, left in enumerate(candidates):
            for right in candidates[i + 1 :]:
                try:
                    if frame.select([left, right]).unique().height == frame.height:
                        keys.append([left, right])
                        if len(keys) >= 3:
                            return keys
                except Exception as exc:  # pragma: no cover - exotic dtype combination
                    log.debug("composite_key_check_failed", columns=[left, right], error=str(exc))
                    continue
        return keys

    def _correlations(self, frame: pl.DataFrame) -> dict[str, dict[str, float]]:
        numeric = [
            name
            for name, dtype in frame.schema.items()
            if is_numeric(dtype) and frame[name].n_unique() > 1
        ][:25]
        if len(numeric) < 2:
            return {}
        matrix = frame.select(numeric).drop_nulls()
        if matrix.height < 3:
            return {}
        array = matrix.to_numpy().astype(float)
        with np.errstate(invalid="ignore", divide="ignore"):
            correlation = np.corrcoef(array, rowvar=False)
        result: dict[str, dict[str, float]] = {}
        for i, left in enumerate(numeric):
            row = {}
            for j, right in enumerate(numeric):
                value = correlation[i][j]
                if not math.isnan(value):
                    row[right] = round(float(value), 4)
            result[left] = row
        return result

    def _recommendations(self, profile: DatasetProfile) -> list[str]:
        notes: list[str] = []
        for column in profile.column_profiles:
            if column.null_ratio > 0.5:
                notes.append(
                    f"'{column.name}' is {column.null_ratio:.0%} null — consider dropping it or "
                    "fixing the upstream source"
                )
            elif column.null_ratio > 0.05:
                notes.append(
                    f"'{column.name}' has {column.null_ratio:.1%} missing values — define a "
                    "filling strategy before aggregating"
                )
            if column.is_constant:
                notes.append(f"'{column.name}' is constant — it carries no information")
            if column.numeric and column.numeric.outlier_count:
                ratio = column.numeric.outlier_count / max(column.count, 1)
                if ratio > 0.01:
                    notes.append(
                        f"'{column.name}' has {column.numeric.outlier_count} statistical outliers "
                        f"({ratio:.1%}) — review before computing averages"
                    )
            if column.text and column.text.whitespace_issues:
                notes.append(
                    f"'{column.name}' has {column.text.whitespace_issues} values with stray "
                    "whitespace — normalise before joining"
                )
            if column.temporal and column.temporal.future_values:
                notes.append(
                    f"'{column.name}' contains {column.temporal.future_values} future dates — "
                    "check timezone handling or data entry"
                )
        if profile.duplicate_rows:
            notes.append(
                f"{profile.duplicate_rows} duplicate rows ({profile.duplicate_ratio:.1%}) — "
                "deduplicate before aggregating"
            )
        if not profile.candidate_keys:
            notes.append("no unique key detected — joins and incremental loads will be ambiguous")
        for left, row in profile.correlations.items():
            for right, value in row.items():
                if left < right and abs(value) > 0.95:
                    notes.append(
                        f"'{left}' and '{right}' are almost perfectly correlated ({value:.2f}) — "
                        "possible redundancy or leakage"
                    )
        return notes[:20]


def discover_relationships(
    frames: dict[str, pl.DataFrame],
    profiles: dict[str, DatasetProfile],
    *,
    min_overlap: float = 0.6,
    max_sample: int = 50_000,
) -> dict[str, list[RelationshipHint]]:
    """Detect foreign-key-like links by value-domain overlap between datasets."""
    hints: dict[str, list[RelationshipHint]] = {name: [] for name in frames}
    keys: dict[str, list[str]] = {
        name: [column for group in profile.candidate_keys for column in group if len(group) == 1]
        for name, profile in profiles.items()
    }

    for left_name, left_frame in frames.items():
        for column in left_frame.columns:
            left_profile = profiles[left_name].column(column)
            if left_profile is None or left_profile.is_constant:
                continue
            if left_profile.semantic_type not in {
                SemanticType.IDENTIFIER,
                SemanticType.CATEGORICAL,
                SemanticType.ORDINAL,
            }:
                continue
            left_values = set(
                left_frame[column]
                .drop_nulls()
                .head(max_sample)
                .cast(pl.Utf8, strict=False)
                .to_list()
            )
            if not left_values:
                continue
            for right_name, right_frame in frames.items():
                if right_name == left_name:
                    continue
                for right_column in keys.get(right_name, []):
                    if right_column not in right_frame.columns:
                        continue
                    right_values = set(
                        right_frame[right_column]
                        .drop_nulls()
                        .head(max_sample)
                        .cast(pl.Utf8, strict=False)
                        .to_list()
                    )
                    if not right_values:
                        continue
                    overlap = len(left_values & right_values) / len(left_values)
                    if overlap >= min_overlap:
                        name_bonus = 0.15 if column == right_column else 0.0
                        hints[left_name].append(
                            RelationshipHint(
                                left_column=column,
                                right_dataset=right_name,
                                right_column=right_column,
                                kind="foreign_key",
                                overlap_ratio=round(overlap, 4),
                                confidence=round(min(overlap + name_bonus, 1.0), 4),
                            )
                        )
    return hints


def _safe(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(result) or math.isinf(result)) else result


def _to_datetime(value: Any) -> Any:
    """Normalise any temporal value to timezone-aware UTC.

    Naive timestamps are *assumed* to be UTC — the alternative (refusing to compare them) would
    make profiling fail on the most common real-world case. The assumption is documented in
    docs/DATA_GOVERNANCE.md and surfaced as a timeliness finding when it matters.
    """
    from datetime import UTC, date, datetime

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _granularity(seconds: float) -> str:
    if seconds <= 1:
        return "second"
    if seconds <= 60:
        return "minute"
    if seconds <= 3600:
        return "hour"
    if seconds <= 86400 * 1.5:
        return "day"
    if seconds <= 86400 * 8:
        return "week"
    if seconds <= 86400 * 32:
        return "month"
    if seconds <= 86400 * 100:
        return "quarter"
    return "year"


_PATTERN_HINTS = {
    "upper_case": r"^[A-Z0-9_\- ]+$",
    "lower_case": r"^[a-z0-9_\- ]+$",
    "numeric_string": r"^\d+$",
    "alphanumeric_code": r"^[A-Z]{2,}\d+$",
}


def _detect_patterns(series: pl.Series) -> list[str]:
    sample = series.head(500)
    if sample.is_empty():
        return []
    found = []
    for label, pattern in _PATTERN_HINTS.items():
        try:
            matches = int(sample.str.contains(pattern).sum())
        except Exception as exc:  # pragma: no cover - pattern unsupported for this dtype
            log.debug("pattern_check_skipped", column=series.name, label=label, error=str(exc))
            continue
        if matches / sample.len() >= 0.9:
            found.append(label)
    return found
