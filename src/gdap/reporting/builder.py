"""Report assembly.

Turns analysis output into a :class:`ReportSpec` — the transport-neutral description of a report.
Rendering happens later, per format, so the same report can be an HTML page, a spreadsheet and a
JSON payload without being rebuilt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gdap.core.contracts import (
    AnalysisResult,
    DatasetProfile,
    Insight,
    QualityReport,
    ReportSection,
    ReportSpec,
)
from gdap.core.enums import InsightKind, ReportFormat, Severity


class ReportBuilder:
    def __init__(
        self,
        title: str,
        *,
        subtitle: str | None = None,
        locale: str = "en_US",
        timezone: str = "UTC",
    ) -> None:
        self._title = title
        self._subtitle = subtitle
        self._locale = locale
        self._timezone = timezone
        self._sections: list[ReportSection] = []
        self._kpis: list[dict[str, Any]] = []
        self._insights: list[Insight] = []
        self._methodology: list[str] = []
        self._metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------ pieces
    def kpi(
        self,
        label: str,
        value: Any,
        *,
        unit: str | None = None,
        change_pct: float | None = None,
        intent: str = "neutral",
    ) -> ReportBuilder:
        self._kpis.append(
            {
                "label": label,
                "value": value,
                "unit": unit,
                "change_pct": change_pct,
                "intent": intent,
            }
        )
        return self

    def section(
        self,
        title: str,
        *,
        body: str | None = None,
        table: list[dict[str, Any]] | None = None,
        charts: list[Any] | None = None,
        insights: list[Insight] | None = None,
        level: int = 2,
    ) -> ReportBuilder:
        self._sections.append(
            ReportSection(
                title=title,
                body=body,
                table=table,
                charts=charts or [],
                insights=insights or [],
                level=level,
            )
        )
        if insights:
            self._insights.extend(insights)
        return self

    def analysis(
        self, result: AnalysisResult, *, title: str | None = None, max_table_rows: int = 50
    ) -> ReportBuilder:
        table = None
        if result.tables:
            first = next(iter(result.tables.values()))
            table = first[:max_table_rows]
        self.section(
            title or f"{result.kind.value.title()} — {result.dataset}",
            body=result.summary,
            table=table,
            charts=result.charts,
            insights=result.insights,
        )
        self._methodology.append(
            f"{result.kind.value}: {', '.join(f'{k}={v}' for k, v in result.params.items()) or 'defaults'}"
        )
        return self

    def profile_section(self, profile: DatasetProfile) -> ReportBuilder:
        table = [
            {
                "column": column.name,
                "type": column.dtype,
                "meaning": column.semantic_type.value,
                "classification": column.classification.value,
                "missing_%": round(column.null_ratio * 100, 2),
                "distinct": column.distinct_count,
                "sample": ", ".join(str(v) for v in column.sample_values[:3]),
            }
            for column in profile.column_profiles
        ]
        body = (
            f"{profile.rows:,} rows × {profile.columns} columns"
            + (f" (profiled on a {profile.sample_rows:,}-row sample)" if profile.sampled else "")
            + f". {profile.duplicate_rows} duplicate row(s)."
        )
        recommendations = "\n".join(f"- {note}" for note in profile.recommendations[:8])
        return self.section(
            "Data profile",
            body=body + ("\n\nRecommendations:\n" + recommendations if recommendations else ""),
            table=table,
        )

    def quality_section(self, report: QualityReport) -> ReportBuilder:
        table = [
            {
                "dimension": dimension.dimension.value,
                "score": dimension.score,
                "weight": round(dimension.weight, 3),
                "failed_checks": dimension.failed,
            }
            for dimension in report.dimensions
        ]
        findings = "\n".join(
            f"- [{finding.severity.value}] {finding.rule}: {finding.message}"
            for finding in report.findings[:10]
        )
        self.kpi(
            "Data quality",
            round(report.score, 1),
            unit="/100",
            intent="good"
            if report.status == "pass"
            else ("warn" if report.status == "warn" else "bad"),
        )
        return self.section(
            f"Data quality — {report.status.upper()} ({report.score:.1f}/100)",
            body=(
                f"{report.expectations_evaluated} expectation(s) evaluated over "
                f"{report.rows_checked:,} rows."
            )
            + ("\n\nFindings:\n" + findings if findings else "\n\nNo findings."),
            table=table,
        )

    def insights_section(
        self, insights: list[Insight], *, title: str = "Insights"
    ) -> ReportBuilder:
        return self.section(title, insights=insights)

    def methodology(self, note: str) -> ReportBuilder:
        self._methodology.append(note)
        return self

    def metadata(self, **values: Any) -> ReportBuilder:
        self._metadata.update(values)
        return self

    # ------------------------------------------------------------------ output
    def build(self, *, formats: list[ReportFormat] | None = None) -> ReportSpec:
        return ReportSpec(
            title=self._title,
            subtitle=self._subtitle,
            formats=formats or [ReportFormat.HTML],
            sections=self._sections,
            executive_summary=self._executive_summary(),
            kpis=self._kpis,
            methodology="\n".join(f"- {note}" for note in self._methodology) or None,
            locale=self._locale,
            timezone=self._timezone,
            generated_at=datetime.now(UTC),
            metadata=self._metadata,
        )

    def _executive_summary(self) -> str | None:
        """Facts first, then inferences, then recommendations — never invented, always sourced."""
        if not self._insights:
            return None
        ordered = sorted(
            self._insights,
            key=lambda insight: (
                _KIND_ORDER.get(insight.kind, 9),
                _SEVERITY_ORDER.get(insight.severity, 9),
                -insight.confidence,
            ),
        )
        lines = []
        for insight in ordered[:6]:
            marker = {
                InsightKind.FACT: "•",
                InsightKind.INFERENCE: "→",
                InsightKind.HYPOTHESIS: "?",
                InsightKind.RECOMMENDATION: "✓",
            }.get(insight.kind, "•")
            lines.append(f"{marker} {insight.title}")
        critical = sum(1 for i in self._insights if i.severity is Severity.CRITICAL)
        if critical:
            lines.append(f"⚠ {critical} critical finding(s) require attention.")
        return "\n".join(lines)


_KIND_ORDER = {
    InsightKind.FACT: 0,
    InsightKind.INFERENCE: 1,
    InsightKind.RECOMMENDATION: 2,
    InsightKind.HYPOTHESIS: 3,
}
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
