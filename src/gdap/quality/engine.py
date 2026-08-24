"""Quality engine (§8).

Produces a 0–100 :class:`QualityReport` from two ingredients:

* **profile-derived measurements** — completeness, uniqueness, consistency, accuracy, timeliness
  and integrity computed from the data itself;
* **explicit expectations** — the rules a team declares for its own domain.

The score is a weighted mean of the dimension scores, with weights configurable per deployment
(``quality.weights``). Nothing about a specific business is hardcoded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

import polars as pl

from gdap.core.config import QualitySettings
from gdap.core.contracts import (
    DatasetProfile,
    DimensionScore,
    Expectation,
    QualityFinding,
    QualityReport,
)
from gdap.core.enums import QualityDimension, SemanticType, Severity
from gdap.observability.logging import get_logger
from gdap.observability.metrics import METRICS
from gdap.quality import expectations as expectation_lib

log = get_logger(__name__)

_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"


class QualityEngine:
    def __init__(self, settings: QualitySettings | None = None) -> None:
        self.settings = settings or QualitySettings()

    # ------------------------------------------------------------------ public API
    def evaluate(
        self,
        frame: pl.DataFrame,
        profile: DatasetProfile,
        *,
        expectations: list[Expectation] | None = None,
        dataset_version_id: str | None = None,
    ) -> QualityReport:
        with METRICS.timer("quality_eval_ms"):
            findings: list[QualityFinding] = []
            evaluated = 0

            for expectation in expectations or []:
                evaluated += 1
                finding = expectation_lib.evaluate(frame, expectation)
                if finding:
                    findings.append(finding)

            measurements = {
                QualityDimension.COMPLETENESS: self._completeness(profile),
                QualityDimension.UNIQUENESS: self._uniqueness(profile),
                QualityDimension.VALIDITY: self._validity(frame, profile, findings),
                QualityDimension.CONSISTENCY: self._consistency(frame, profile, findings),
                QualityDimension.ACCURACY: self._accuracy(profile, findings),
                QualityDimension.TIMELINESS: self._timeliness(profile, findings),
                QualityDimension.INTEGRITY: self._integrity(profile, findings),
            }

            # explicit expectation failures pull their own dimension down
            for finding in findings:
                if finding.severity is Severity.INFO:
                    continue
                penalty = (
                    100 * finding.failed_ratio
                    if finding.failed_ratio
                    else (25 if finding.severity is Severity.WARNING else 60)
                )
                current = measurements[finding.dimension]
                measurements[finding.dimension] = max(current - penalty, 0.0)

            weights = self._normalised_weights()
            dimensions = [
                DimensionScore(
                    dimension=dimension,
                    score=round(max(0.0, min(100.0, value)), 2),
                    weight=weights[dimension.value],
                    checks=sum(1 for f in findings if f.dimension is dimension) or 1,
                    failed=sum(1 for f in findings if f.dimension is dimension),
                )
                for dimension, value in measurements.items()
            ]
            score = round(sum(d.score * d.weight for d in dimensions), 2)
            status = self._status(score, findings)

            report = QualityReport(
                dataset=profile.dataset,
                dataset_version_id=dataset_version_id or profile.dataset_version_id,
                score=score,
                status=status,
                dimensions=dimensions,
                findings=sorted(
                    findings,
                    key=lambda f: (_severity_rank(f.severity), -f.failed_ratio),
                ),
                rows_checked=frame.height,
                expectations_evaluated=evaluated,
                evaluated_at=datetime.now(UTC),
            )
            log.info(
                "quality_evaluated",
                dataset=profile.dataset,
                score=score,
                status=status,
                findings=len(findings),
            )
            METRICS.gauge("quality_score", score, dataset=profile.dataset)
            return report

    def gate(self, report: QualityReport, *, minimum: float | None = None) -> None:
        """Raise :class:`QualityGateError` when a report is below the acceptable threshold."""
        from gdap.core.errors import QualityGateError

        threshold = minimum if minimum is not None else self.settings.fail_below_score
        if report.score < threshold:
            raise QualityGateError(
                f"quality gate failed for '{report.dataset}': {report.score:.1f} < {threshold:.1f}",
                details={
                    "score": report.score,
                    "threshold": threshold,
                    "critical_findings": [f.message for f in report.critical][:10],
                },
            )

    # ------------------------------------------------------------------ dimensions
    def _completeness(self, profile: DatasetProfile) -> float:
        if not profile.column_profiles:
            return 100.0
        filled = [1 - column.null_ratio for column in profile.column_profiles]
        return 100.0 * sum(filled) / len(filled)

    def _uniqueness(self, profile: DatasetProfile) -> float:
        score = 100.0 - 100.0 * profile.duplicate_ratio
        if not profile.candidate_keys and profile.rows > 0:
            score -= 10.0  # no identifiable key is a real, if soft, defect
        return score

    def _validity(
        self, frame: pl.DataFrame, profile: DatasetProfile, findings: list[QualityFinding]
    ) -> float:
        invalid_ratios: list[float] = []
        for column in profile.column_profiles:
            if column.name not in frame.columns or column.count == 0:
                continue
            series = frame[column.name].drop_nulls()
            if series.is_empty():
                continue
            invalid = 0
            if column.semantic_type is SemanticType.EMAIL:
                invalid = int((~series.cast(pl.Utf8, strict=False).str.contains(_EMAIL_RE)).sum())
                if invalid:
                    findings.append(
                        QualityFinding(
                            dimension=QualityDimension.VALIDITY,
                            severity=Severity.WARNING,
                            column=column.name,
                            rule="semantic:email",
                            message=f"{invalid} values are not valid e-mail addresses",
                            failed_rows=invalid,
                            failed_ratio=invalid / series.len(),
                            suggestion="validate at the point of capture",
                        )
                    )
            elif column.semantic_type in {SemanticType.CURRENCY, SemanticType.QUANTITY}:
                if column.numeric and column.numeric.negatives:
                    invalid = column.numeric.negatives
                    findings.append(
                        QualityFinding(
                            dimension=QualityDimension.VALIDITY,
                            severity=Severity.WARNING,
                            column=column.name,
                            rule="semantic:non_negative",
                            message=(
                                f"{invalid} negative values in a "
                                f"{column.semantic_type.value} column"
                            ),
                            failed_rows=invalid,
                            failed_ratio=invalid / max(column.count, 1),
                            suggestion="confirm whether refunds/returns are expected here",
                        )
                    )
            elif column.semantic_type is SemanticType.PERCENTAGE and column.numeric:
                maximum = column.numeric.max or 0
                if maximum > 100 or (column.numeric.min or 0) < 0:
                    invalid = 1
                    findings.append(
                        QualityFinding(
                            dimension=QualityDimension.VALIDITY,
                            severity=Severity.WARNING,
                            column=column.name,
                            rule="semantic:percentage_bounds",
                            message=f"percentage values outside 0–100 (max={maximum})",
                            failed_rows=0,
                            failed_ratio=0.02,
                            suggestion="check whether the value is a ratio (0–1) instead",
                        )
                    )
            invalid_ratios.append(invalid / max(series.len(), 1))
        if not invalid_ratios:
            return 100.0
        return 100.0 * (1 - sum(invalid_ratios) / len(invalid_ratios))

    def _consistency(
        self, frame: pl.DataFrame, profile: DatasetProfile, findings: list[QualityFinding]
    ) -> float:
        penalties = 0.0
        for column in profile.column_profiles:
            if column.text is None or column.name not in frame.columns:
                continue
            if column.text.whitespace_issues:
                ratio = column.text.whitespace_issues / max(column.count, 1)
                penalties += ratio * 40
                findings.append(
                    QualityFinding(
                        dimension=QualityDimension.CONSISTENCY,
                        severity=Severity.INFO,
                        column=column.name,
                        rule="format:whitespace",
                        message=f"{column.text.whitespace_issues} values have leading/trailing spaces",
                        failed_rows=column.text.whitespace_issues,
                        failed_ratio=ratio,
                        suggestion="trim on ingestion",
                    )
                )
            if column.semantic_type is SemanticType.CATEGORICAL:
                variants = _case_variants(frame[column.name])
                if variants:
                    penalties += 10
                    findings.append(
                        QualityFinding(
                            dimension=QualityDimension.CONSISTENCY,
                            severity=Severity.WARNING,
                            column=column.name,
                            rule="format:case_variants",
                            message=(
                                f"{len(variants)} categories differ only by case/spacing: "
                                + ", ".join(list(variants)[:3])
                            ),
                            failed_rows=len(variants),
                            failed_ratio=0.0,
                            sample=list(variants)[:5],
                            suggestion="normalise category casing",
                        )
                    )
        return max(100.0 - penalties, 0.0)

    def _accuracy(self, profile: DatasetProfile, findings: list[QualityFinding]) -> float:
        outlier_ratios = []
        for column in profile.column_profiles:
            if column.numeric and column.count:
                ratio = column.numeric.outlier_count / column.count
                outlier_ratios.append(min(ratio, 0.5))
                if ratio > 0.05:
                    findings.append(
                        QualityFinding(
                            dimension=QualityDimension.ACCURACY,
                            severity=Severity.INFO,
                            column=column.name,
                            rule="statistical:outliers",
                            message=(
                                f"{column.numeric.outlier_count} outliers ({ratio:.1%}) beyond "
                                "1.5×IQR"
                            ),
                            failed_rows=column.numeric.outlier_count,
                            failed_ratio=ratio,
                            suggestion="use median/robust statistics or investigate the tail",
                        )
                    )
        if not outlier_ratios:
            return 100.0
        return 100.0 * (1 - sum(outlier_ratios) / len(outlier_ratios))

    def _timeliness(self, profile: DatasetProfile, findings: list[QualityFinding]) -> float:
        temporal = [c for c in profile.column_profiles if c.temporal and c.temporal.max]
        if not temporal:
            return 100.0
        candidates = [
            (c.temporal.max if c.temporal.max.tzinfo else c.temporal.max.replace(tzinfo=UTC))
            for c in temporal
            if c.temporal and c.temporal.max
        ]
        if not candidates:
            return 100.0
        newest = max(candidates)
        age_days = (datetime.now(UTC) - newest).total_seconds() / 86400
        future = sum(c.temporal.future_values for c in temporal if c.temporal)
        if future:
            findings.append(
                QualityFinding(
                    dimension=QualityDimension.TIMELINESS,
                    severity=Severity.WARNING,
                    rule="temporal:future_values",
                    message=f"{future} timestamps are in the future",
                    failed_rows=future,
                    failed_ratio=future / max(profile.rows, 1),
                    suggestion="check timezone conversion at the source",
                )
            )
        if age_days <= 1:
            return 100.0
        if age_days <= 7:
            return 95.0
        if age_days <= 30:
            return 85.0
        if age_days <= 90:
            return 70.0
        return 50.0

    def _integrity(self, profile: DatasetProfile, findings: list[QualityFinding]) -> float:
        score = 100.0
        if not profile.candidate_keys:
            score -= 15.0
        for column in profile.column_profiles:
            if column.is_constant and profile.rows > 1:
                score -= 3.0
        for relationship in profile.relationships:
            if relationship.overlap_ratio < 1.0:
                orphan_ratio = 1 - relationship.overlap_ratio
                score -= min(orphan_ratio * 50, 25)
                findings.append(
                    QualityFinding(
                        dimension=QualityDimension.INTEGRITY,
                        severity=Severity.WARNING,
                        column=relationship.left_column,
                        rule="referential:orphans",
                        message=(
                            f"{orphan_ratio:.1%} of '{relationship.left_column}' values have no "
                            f"match in {relationship.right_dataset}.{relationship.right_column}"
                        ),
                        failed_rows=int(orphan_ratio * profile.rows),
                        failed_ratio=orphan_ratio,
                        suggestion="load the parent dataset first or fix the join key",
                    )
                )
        return max(score, 0.0)

    # ------------------------------------------------------------------ helpers
    def _normalised_weights(self) -> dict[str, float]:
        weights = dict(self.settings.weights)
        for dimension in QualityDimension:
            weights.setdefault(dimension.value, 0.0)
        total = sum(weights.values()) or 1.0
        return {key: value / total for key, value in weights.items()}

    def _status(
        self, score: float, findings: list[QualityFinding]
    ) -> Literal["pass", "warn", "fail"]:
        if score < self.settings.fail_below_score or any(
            f.severity is Severity.CRITICAL for f in findings
        ):
            return "fail"
        if score < self.settings.warn_below_score:
            return "warn"
        return "pass"


def suggest_expectations(profile: DatasetProfile) -> list[Expectation]:
    """Derive a starting expectation suite from a profile — the user then edits it."""
    suggestions: list[Expectation] = []
    for column in profile.column_profiles:
        if column.null_ratio == 0 and column.count > 0:
            suggestions.append(
                Expectation(
                    column=column.name,
                    kind="not_null",
                    dimension=QualityDimension.COMPLETENESS,
                    severity=Severity.CRITICAL,
                    description="column was fully populated when profiled",
                )
            )
        if column.is_candidate_key:
            suggestions.append(
                Expectation(
                    column=column.name,
                    kind="unique",
                    dimension=QualityDimension.UNIQUENESS,
                    severity=Severity.CRITICAL,
                    description="column looked like a key when profiled",
                )
            )
        if column.numeric and column.numeric.min is not None:
            margin = abs(column.numeric.max - column.numeric.min) * 0.5 or 1.0  # type: ignore[operator]
            suggestions.append(
                Expectation(
                    column=column.name,
                    kind="in_range",
                    params={
                        "min": round(column.numeric.min - margin, 6),
                        "max": round(column.numeric.max + margin, 6),  # type: ignore[operator]
                    },
                    dimension=QualityDimension.VALIDITY,
                    severity=Severity.WARNING,
                    description="range observed at profiling time, widened by 50%",
                )
            )
        if (
            column.semantic_type is SemanticType.CATEGORICAL
            and 0 < column.distinct_count <= 25
            and column.top_values
        ):
            suggestions.append(
                Expectation(
                    column=column.name,
                    kind="in_set",
                    params={"values": [value for value, _ in column.top_values]},
                    dimension=QualityDimension.VALIDITY,
                    severity=Severity.WARNING,
                    description="categories observed at profiling time",
                )
            )
    if profile.rows:
        suggestions.append(
            Expectation(
                kind="row_count_between",
                params={"min": max(int(profile.rows * 0.5), 1), "max": int(profile.rows * 3)},
                dimension=QualityDimension.COMPLETENESS,
                severity=Severity.WARNING,
                description="volume envelope based on the profiled load",
            )
        )
    return suggestions


def _case_variants(series: pl.Series) -> set[str]:
    """Categories that collapse onto each other once case/whitespace is normalised."""
    try:
        values = [str(v) for v in series.drop_nulls().unique().head(2000).to_list()]
    except Exception:  # pragma: no cover
        return set()
    buckets: dict[str, set[str]] = {}
    for value in values:
        key = re.sub(r"\s+", " ", value.strip().lower())
        buckets.setdefault(key, set()).add(value)
    return {value for group in buckets.values() if len(group) > 1 for value in group}


def _severity_rank(severity: Severity) -> int:
    return {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}[severity]


def _default_settings() -> QualitySettings:
    return QualitySettings()


__all__ = ["QualityEngine", "suggest_expectations"]
