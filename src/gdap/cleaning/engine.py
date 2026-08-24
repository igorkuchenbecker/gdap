"""Cleaning engine (§9).

Two strictly separated phases:

``propose(frame, profile, quality)``
    Pure analysis. Returns :class:`CleaningProposal` objects — each with the issue it addresses,
    the action it would take, how many rows it touches, whether it is reversible, and whether it
    is deterministic or AI-suggested.

``apply(frame, proposals, ...)``
    Executes only the proposals the policy engine (and, where required, a human) approved. Every
    applied action is recorded in the returned :class:`CleaningResult`, which is what the audit
    trail and the report show.

No transformation is ever applied as a side effect of profiling or validation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import polars as pl

from gdap.core.contracts import (
    CleaningProposal,
    CleaningResult,
    DatasetProfile,
    Principal,
    QualityReport,
)
from gdap.core.enums import ApprovalMode, DataClassification, SemanticType
from gdap.core.errors import ValidationFailedError
from gdap.governance.policy import PolicyEngine
from gdap.observability.logging import get_logger

log = get_logger(__name__)

_NUMERIC_RE = re.compile(r"^-?[\d.,\s]+%?$")


class CleaningEngine:
    def __init__(
        self,
        *,
        null_ratio_drop_threshold: float = 0.7,
        max_fill_ratio: float = 0.35,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.null_ratio_drop_threshold = null_ratio_drop_threshold
        self.max_fill_ratio = max_fill_ratio
        self.policy = policy or PolicyEngine()

    # ------------------------------------------------------------------ propose
    def propose(
        self,
        frame: pl.DataFrame,
        profile: DatasetProfile,
        quality: QualityReport | None = None,
    ) -> list[CleaningProposal]:
        proposals: list[CleaningProposal] = []
        counter = 0

        def add(**values: Any) -> None:
            nonlocal counter
            counter += 1
            proposals.append(CleaningProposal(id=f"fix-{counter:03d}", **values))

        if profile.duplicate_rows:
            add(
                column=None,
                issue=f"{profile.duplicate_rows} duplicate rows ({profile.duplicate_ratio:.2%})",
                action="drop_duplicates",
                params={"subset": None},
                affected_rows=profile.duplicate_rows,
                reversible=False,
                approval=ApprovalMode.AUTO
                if profile.duplicate_ratio < 0.1
                else ApprovalMode.REQUIRES_APPROVAL,
                rationale="exact duplicates distort every aggregate",
            )

        for column in profile.column_profiles:
            if column.name not in frame.columns:
                continue
            series = frame[column.name]

            if column.text and column.text.whitespace_issues:
                add(
                    column=column.name,
                    issue=f"{column.text.whitespace_issues} values with stray whitespace",
                    action="trim_whitespace",
                    affected_rows=column.text.whitespace_issues,
                    rationale="whitespace silently breaks joins and grouping",
                )

            if column.semantic_type is SemanticType.CATEGORICAL:
                variants = _case_variants(series)
                if variants:
                    add(
                        column=column.name,
                        issue=f"{len(variants)} category spellings differ only by case/spacing",
                        action="normalize_categories",
                        params={"variants": sorted(variants)[:50]},
                        affected_rows=int(series.is_in(list(variants)).sum()),
                        approval=ApprovalMode.AUTO_WITH_VALIDATION,
                        rationale="collapses split categories onto the most frequent spelling",
                    )

            if column.is_constant and profile.rows > 1:
                add(
                    column=column.name,
                    issue="column is constant",
                    action="drop_column",
                    affected_rows=profile.rows,
                    reversible=False,
                    approval=ApprovalMode.REQUIRES_APPROVAL,
                    rationale="carries no information, but may be required downstream",
                )
            elif column.null_ratio >= self.null_ratio_drop_threshold:
                add(
                    column=column.name,
                    issue=f"{column.null_ratio:.0%} missing",
                    action="drop_column",
                    affected_rows=column.null_count,
                    reversible=False,
                    approval=ApprovalMode.REQUIRES_APPROVAL,
                    rationale="too sparse to analyse; confirm with the data owner first",
                )
            elif 0 < column.null_ratio <= self.max_fill_ratio:
                strategy, detail = self._fill_strategy(column)
                if strategy:
                    add(
                        column=column.name,
                        issue=f"{column.null_count} missing values ({column.null_ratio:.1%})",
                        action="fill_missing",
                        params={"strategy": strategy, **detail},
                        affected_rows=column.null_count,
                        approval=ApprovalMode.AUTO
                        if column.null_ratio < 0.05
                        else ApprovalMode.AUTO_WITH_VALIDATION,
                        rationale=f"{strategy} preserves the distribution better than dropping rows",
                    )

            if (
                column.dtype.startswith("String")
                and column.semantic_type
                in {SemanticType.NUMERIC, SemanticType.CURRENCY, SemanticType.QUANTITY}
                and _parsable_as_number(series)
            ):
                add(
                    column=column.name,
                    issue="numeric values stored as text",
                    action="cast_numeric",
                    affected_rows=int(series.drop_nulls().len()),
                    rationale="text numbers cannot be aggregated or compared",
                )

            if column.numeric and column.numeric.outlier_count and column.count:
                ratio = column.numeric.outlier_count / column.count
                if ratio > 0.005:
                    bounds = column.numeric.outlier_bounds
                    add(
                        column=column.name,
                        issue=f"{column.numeric.outlier_count} outliers beyond 1.5×IQR",
                        action="clip_outliers",
                        params={
                            "lower": bounds[0] if bounds else None,
                            "upper": bounds[1] if bounds else None,
                        },
                        affected_rows=column.numeric.outlier_count,
                        reversible=False,
                        approval=ApprovalMode.REQUIRES_APPROVAL,
                        confidence=0.6,
                        rationale=(
                            "extreme values may be genuine (a large order) or an error — "
                            "a human decides"
                        ),
                    )

        for finding in quality.findings if quality else []:
            if finding.rule == "semantic:email" and finding.column:
                add(
                    column=finding.column,
                    issue=finding.message,
                    action="flag_invalid",
                    params={
                        "pattern": r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$",
                        "flag_column": f"{finding.column}_is_valid",
                    },
                    affected_rows=finding.failed_rows,
                    rationale="flagging keeps the rows auditable instead of deleting them",
                )

        log.info("cleaning_proposed", dataset=profile.dataset, proposals=len(proposals))
        return proposals

    # ------------------------------------------------------------------ apply
    def apply(
        self,
        frame: pl.DataFrame,
        proposals: list[CleaningProposal],
        *,
        principal: Principal | None = None,
        classification: DataClassification = DataClassification.INTERNAL,
        allow_modes: set[ApprovalMode] | None = None,
        approved_ids: set[str] | None = None,
    ) -> tuple[pl.DataFrame, CleaningResult]:
        """Apply approved proposals only. Anything requiring a human is returned as skipped."""
        allowed = allow_modes or {ApprovalMode.AUTO, ApprovalMode.AUTO_WITH_VALIDATION}
        approved_ids = approved_ids or set()
        result = CleaningResult(rows_before=frame.height, rows_after=frame.height)
        working = frame

        for proposal in proposals:
            gate = proposal.approval
            if principal is not None:
                decision = self.policy.decide(
                    principal,
                    "clean.apply",
                    classification=classification,
                    affected_ratio=proposal.affected_rows / max(frame.height, 1),
                    origin=proposal.origin,
                )
                if not decision.allowed:
                    result.skipped.append(proposal)
                    result.log.append(f"{proposal.id}: blocked by policy — {decision.reason}")
                    continue
                gate = _strictest(gate, decision.approval)

            if gate not in allowed and proposal.id not in approved_ids:
                result.skipped.append(proposal)
                result.log.append(
                    f"{proposal.id}: requires approval ({gate.value}) — {proposal.action}"
                    f"{' on ' + proposal.column if proposal.column else ''}"
                )
                continue

            action = _ACTIONS.get(proposal.action)
            if action is None:
                result.skipped.append(proposal)
                result.log.append(f"{proposal.id}: unknown action '{proposal.action}'")
                continue

            before_rows = working.height
            try:
                working, changed = action(working, proposal)
            except Exception as exc:
                result.skipped.append(proposal)
                result.log.append(f"{proposal.id}: failed — {exc}")
                log.warning("cleaning_action_failed", action=proposal.action, error=str(exc))
                continue

            result.applied.append(proposal)
            result.cells_changed += changed
            result.log.append(
                f"{proposal.id}: {proposal.action}"
                f"{' on ' + proposal.column if proposal.column else ''} "
                f"→ {changed} cell(s), rows {before_rows}→{working.height}"
            )

        result.rows_after = working.height
        log.info(
            "cleaning_applied",
            applied=len(result.applied),
            skipped=len(result.skipped),
            rows_before=result.rows_before,
            rows_after=result.rows_after,
        )
        return working, result

    # ------------------------------------------------------------------ helpers
    def _fill_strategy(self, column: Any) -> tuple[str | None, dict[str, Any]]:
        if column.numeric is not None:
            skew = abs(column.numeric.skewness or 0)
            return ("median" if skew > 0.5 else "mean"), {}
        if column.semantic_type in {SemanticType.CATEGORICAL, SemanticType.ORDINAL}:
            if column.top_values:
                return "mode", {"value": column.top_values[0][0]}
            return None, {}
        if column.temporal is not None:
            return "forward_fill", {}
        if column.semantic_type in {SemanticType.FREE_TEXT, SemanticType.IDENTIFIER}:
            return "constant", {"value": "unknown"}
        return None, {}


# ─────────────────────────────────────── action implementations ────────────────────────────


def _drop_duplicates(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    subset = proposal.params.get("subset")
    before = frame.height
    cleaned = frame.unique(subset=subset, keep="first", maintain_order=True)
    return cleaned, before - cleaned.height


def _trim_whitespace(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    original = frame[column]
    trimmed = original.cast(pl.Utf8, strict=False).str.strip_chars()
    changed = int((original.cast(pl.Utf8, strict=False) != trimmed).sum())
    return frame.with_columns(trimmed.alias(column)), changed


def _normalize_categories(
    frame: pl.DataFrame, proposal: CleaningProposal
) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    series = frame[column].cast(pl.Utf8, strict=False)
    canonical = _canonical_map(series)
    if not canonical:
        return frame, 0
    replaced = series.replace(canonical)
    changed = int((series != replaced).sum())
    return frame.with_columns(replaced.alias(column)), changed


def _fill_missing(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    strategy = proposal.params.get("strategy", "mean")
    series = frame[column]
    missing = int(series.null_count())
    if missing == 0:
        return frame, 0

    if strategy == "mean":
        filled = series.fill_null(series.mean())
    elif strategy == "median":
        filled = series.fill_null(series.median())
    elif strategy == "mode":
        value = proposal.params.get("value")
        if value is None:
            modes = series.drop_nulls().mode()
            value = modes[0] if modes.len() else None
        filled = series.fill_null(value)
    elif strategy == "forward_fill":
        filled = series.fill_null(strategy="forward")
    elif strategy == "constant":
        filled = series.fill_null(proposal.params.get("value", "unknown"))
    elif strategy == "zero":
        filled = series.fill_null(0)
    else:
        raise ValidationFailedError(f"unknown fill strategy '{strategy}'")
    return frame.with_columns(filled.alias(column)), missing


def _drop_column(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    if column not in frame.columns:
        return frame, 0
    return frame.drop(column), frame.height


def _cast_numeric(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    """Parse text numbers, honouring the locale convention detected in the column (§43)."""
    column = proposal.column or ""
    text = frame[column].cast(pl.Utf8, strict=False).str.replace_all(r"[^\d.,\-]", "")
    convention = proposal.params.get("decimal") or _detect_decimal_convention(text)
    if convention == "comma":  # 1.234,56 → 1234.56
        normalised = text.str.replace_all(r"\.", "").str.replace_all(",", ".")
    else:  # 1,234.56 → 1234.56
        normalised = text.str.replace_all(",", "")
    cleaned = normalised.cast(pl.Float64, strict=False)
    changed = int(cleaned.is_not_null().sum())
    return frame.with_columns(cleaned.alias(column)), changed


def _detect_decimal_convention(series: pl.Series) -> str:
    """``comma`` for 1.234,56 (most of Europe/LatAm), ``dot`` for 1,234.56."""
    sample = [str(v) for v in series.drop_nulls().head(200).to_list()]
    comma_decimal = sum(1 for v in sample if re.search(r",\d{1,2}$", v))
    dot_decimal = sum(1 for v in sample if re.search(r"\.\d{1,2}$", v))
    return "comma" if comma_decimal > dot_decimal else "dot"


def _clip_outliers(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    lower = proposal.params.get("lower")
    upper = proposal.params.get("upper")
    if lower is None or upper is None:
        return frame, 0
    series = frame[column]
    changed = int(((series < lower) | (series > upper)).sum())
    return frame.with_columns(series.clip(lower, upper).alias(column)), changed


def _flag_invalid(frame: pl.DataFrame, proposal: CleaningProposal) -> tuple[pl.DataFrame, int]:
    column = proposal.column or ""
    pattern = str(proposal.params.get("pattern", ".*"))
    flag_column = str(proposal.params.get("flag_column", f"{column}_is_valid"))
    valid = frame[column].cast(pl.Utf8, strict=False).str.contains(pattern).fill_null(False)
    return frame.with_columns(valid.alias(flag_column)), int((~valid).sum())


_ACTIONS: dict[str, Callable[[pl.DataFrame, CleaningProposal], tuple[pl.DataFrame, int]]] = {
    "drop_duplicates": _drop_duplicates,
    "trim_whitespace": _trim_whitespace,
    "normalize_categories": _normalize_categories,
    "fill_missing": _fill_missing,
    "drop_column": _drop_column,
    "cast_numeric": _cast_numeric,
    "clip_outliers": _clip_outliers,
    "flag_invalid": _flag_invalid,
}


def _case_variants(series: pl.Series) -> set[str]:
    try:
        values = [str(v) for v in series.drop_nulls().unique().head(2000).to_list()]
    except Exception:  # pragma: no cover
        return set()
    buckets: dict[str, set[str]] = {}
    for value in values:
        buckets.setdefault(re.sub(r"\s+", " ", value.strip().lower()), set()).add(value)
    return {value for group in buckets.values() if len(group) > 1 for value in group}


def _canonical_map(series: pl.Series) -> dict[str, str]:
    """Map every spelling variant to the most frequent one in its normalised bucket."""
    counts = series.drop_nulls().value_counts(sort=True)
    if counts.is_empty():
        return {}
    value_column, count_column = counts.columns[0], counts.columns[1]
    buckets: dict[str, list[tuple[str, int]]] = {}
    for row in counts.iter_rows(named=True):
        value = str(row[value_column])
        key = re.sub(r"\s+", " ", value.strip().lower())
        buckets.setdefault(key, []).append((value, int(row[count_column])))
    mapping: dict[str, str] = {}
    for group in buckets.values():
        if len(group) <= 1:
            continue
        winner = max(group, key=lambda item: item[1])[0]
        for value, _ in group:
            if value != winner:
                mapping[value] = winner
    return mapping


def _parsable_as_number(series: pl.Series, *, threshold: float = 0.95) -> bool:
    sample = series.drop_nulls().head(500)
    if sample.is_empty():
        return False
    values = [str(v) for v in sample.to_list()]
    hits = sum(1 for value in values if _NUMERIC_RE.match(value.strip()))
    return hits / len(values) >= threshold


def _strictest(left: ApprovalMode, right: ApprovalMode) -> ApprovalMode:
    order = [
        ApprovalMode.AUTO,
        ApprovalMode.AUTO_WITH_VALIDATION,
        ApprovalMode.REQUIRES_APPROVAL,
        ApprovalMode.BLOCKED,
    ]
    return max([left, right], key=order.index)
