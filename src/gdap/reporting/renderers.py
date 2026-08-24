"""Report renderers (§23): HTML, Markdown, JSON, CSV, XLSX and PDF.

Each renderer takes the same :class:`ReportSpec` and returns bytes. HTML is fully self-contained
(styles and chart JS inlined) so an artifact can be e-mailed, archived or served from object
storage without any runtime dependency.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape

from gdap.core.contracts import ReportSpec
from gdap.core.enums import ReportFormat
from gdap.core.errors import UnsupportedOperationError
from gdap.observability.logging import get_logger
from gdap.reporting.charts import to_html as chart_to_html

log = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


class Renderer(Protocol):
    extension: str
    media_type: str

    def render(self, spec: ReportSpec) -> bytes: ...


class HtmlRenderer:
    """Self-contained by default.

    ``plotly_js="inline"`` (default) embeds the chart library, producing a file that renders with
    no network at all — at ~4 MB. ``"cdn"`` keeps artifacts small for intranet dashboards where
    the browser can reach a CDN. ``"none"`` is for embedding into a page that already loads it.
    """

    extension = "html"
    media_type = "text/html"

    def __init__(self, plotly_js: str = "inline") -> None:
        self.plotly_js = plotly_js
        self._env = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._env.filters["number"] = _format_number
        self._env.filters["datetime"] = _format_datetime

    def render(self, spec: ReportSpec) -> bytes:
        rendered_sections: list[dict[str, Any]] = []
        first_chart = True
        for section in spec.sections:
            charts_html = []
            for index, chart in enumerate(section.charts):
                charts_html.append(
                    chart_to_html(
                        chart,
                        include_js=first_chart and self.plotly_js == "inline",
                        div_id=f"chart-{len(rendered_sections)}-{index}",
                    )
                )
                first_chart = False
            rendered_sections.append(
                {
                    "section": section,
                    "charts_html": charts_html,
                    "columns": list(section.table[0].keys()) if section.table else [],
                }
            )
        template = self._env.get_template("report.html.j2")
        html = template.render(
            spec=spec,
            sections=rendered_sections,
            generated_at=spec.generated_at,
            plotly_cdn=self.plotly_js == "cdn",
        )
        return html.encode("utf-8")


class MarkdownRenderer:
    extension = "md"
    media_type = "text/markdown"

    def render(self, spec: ReportSpec) -> bytes:
        lines = [f"# {spec.title}"]
        if spec.subtitle:
            lines.append(f"_{spec.subtitle}_")
        lines.append(f"\nGenerated {_format_datetime(spec.generated_at)} ({spec.timezone})\n")

        if spec.kpis:
            lines.append("## Key indicators\n")
            lines.append("| Indicator | Value | Change |")
            lines.append("|---|---:|---:|")
            for kpi in spec.kpis:
                change = f"{kpi['change_pct']:+.1f}%" if kpi.get("change_pct") is not None else "—"
                lines.append(
                    f"| {kpi['label']} | {_format_number(kpi['value'])}{kpi.get('unit') or ''} | {change} |"
                )
            lines.append("")

        if spec.executive_summary:
            lines.append("## Executive summary\n")
            lines.append(spec.executive_summary + "\n")

        for section in spec.sections:
            lines.append(f"{'#' * min(section.level, 6)} {section.title}\n")
            if section.body:
                lines.append(section.body + "\n")
            for insight in section.insights:
                lines.append(
                    f"- **[{insight.kind.value}]** {insight.title} "
                    f"(confidence {insight.confidence:.0%})"
                )
                if insight.detail:
                    lines.append(f"  - {insight.detail}")
            if section.table:
                columns = list(section.table[0].keys())
                lines.append("")
                lines.append("| " + " | ".join(columns) + " |")
                lines.append("|" + "|".join(["---"] * len(columns)) + "|")
                for row in section.table[:100]:
                    lines.append(
                        "| " + " | ".join(_stringify(row.get(column)) for column in columns) + " |"
                    )
            if section.charts:
                lines.append("")
                for chart in section.charts:
                    lines.append(f"_[chart] {chart.title} ({chart.kind})_")
            lines.append("")

        if spec.methodology:
            lines.append("## Methodology\n")
            lines.append(spec.methodology)
        return "\n".join(lines).encode("utf-8")


class JsonRenderer:
    extension = "json"
    media_type = "application/json"

    def render(self, spec: ReportSpec) -> bytes:
        return json.dumps(spec.model_dump(mode="json"), indent=2, default=str).encode("utf-8")


class CsvRenderer:
    """Flattens every table in the report, prefixed by its section — spreadsheet-friendly."""

    extension = "csv"
    media_type = "text/csv"

    def render(self, spec: ReportSpec) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["report", spec.title])
        writer.writerow(["generated_at", spec.generated_at.isoformat()])
        writer.writerow([])
        for kpi in spec.kpis:
            writer.writerow(["kpi", kpi["label"], kpi["value"], kpi.get("unit") or ""])
        for section in spec.sections:
            if not section.table:
                continue
            writer.writerow([])
            writer.writerow([f"section: {section.title}"])
            columns = list(section.table[0].keys())
            writer.writerow(columns)
            for row in section.table:
                writer.writerow([_stringify(row.get(column)) for column in columns])
        return buffer.getvalue().encode("utf-8")


class XlsxRenderer:
    extension = "xlsx"
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def render(self, spec: ReportSpec) -> bytes:
        import xlsxwriter

        buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(
            buffer, {"in_memory": True, "default_date_format": "yyyy-mm-dd"}
        )
        title_format = workbook.add_format({"bold": True, "font_size": 14})
        header_format = workbook.add_format(
            {"bold": True, "bg_color": "#0072B2", "font_color": "white", "border": 1}
        )
        wrap_format = workbook.add_format({"text_wrap": True, "valign": "top"})

        summary = workbook.add_worksheet("Summary")
        summary.set_column(0, 0, 34)
        summary.set_column(1, 3, 22)
        summary.write(0, 0, spec.title, title_format)
        summary.write(1, 0, spec.subtitle or "")
        summary.write(2, 0, "Generated")
        summary.write(2, 1, spec.generated_at.strftime("%Y-%m-%d %H:%M:%S"))
        row = 4
        if spec.kpis:
            summary.write_row(row, 0, ["Indicator", "Value", "Unit", "Change %"], header_format)
            row += 1
            for kpi in spec.kpis:
                summary.write_row(
                    row,
                    0,
                    [
                        kpi["label"],
                        _numeric_or_text(kpi["value"]),
                        kpi.get("unit") or "",
                        kpi.get("change_pct") if kpi.get("change_pct") is not None else "",
                    ],
                )
                row += 1
        if spec.executive_summary:
            row += 1
            summary.write(row, 0, "Executive summary", header_format)
            summary.merge_range(row + 1, 0, row + 6, 3, spec.executive_summary, wrap_format)

        used_names: set[str] = {"Summary"}
        for index, section in enumerate(spec.sections):
            if not section.table:
                continue
            sheet_name = _sheet_name(section.title, index, used_names)
            sheet = workbook.add_worksheet(sheet_name)
            columns = list(section.table[0].keys())
            sheet.write_row(0, 0, columns, header_format)
            for row_index, record in enumerate(section.table, start=1):
                sheet.write_row(
                    row_index, 0, [_numeric_or_text(record.get(column)) for column in columns]
                )
            sheet.set_column(0, max(len(columns) - 1, 0), 20)
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, len(section.table), len(columns) - 1)

        workbook.close()
        return buffer.getvalue()


class PdfRenderer:
    """HTML → PDF.

    PDF generation needs a real layout engine. Rather than shipping a fake, this renderer uses
    WeasyPrint when it is installed and otherwise fails with an actionable message (§63).
    """

    extension = "pdf"
    media_type = "application/pdf"

    def render(self, spec: ReportSpec) -> bytes:
        html = HtmlRenderer().render(spec)
        try:
            from weasyprint import HTML  # type: ignore[import-not-found]
        except ImportError as exc:
            raise UnsupportedOperationError(
                "PDF rendering requires WeasyPrint: pip install weasyprint "
                "(HTML, XLSX, CSV, JSON and Markdown are available without it)",
                details={"format": "pdf", "available": [f.value for f in RENDERERS]},
            ) from exc
        return HTML(string=html.decode("utf-8")).write_pdf()  # pragma: no cover - optional dep


# ─────────────────────────────────────────── helpers ───────────────────────────────────────


def _format_number(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        number = float(value)
        if abs(number) >= 1_000_000_000:
            return f"{number / 1_000_000_000:.{decimals}f}B"
        if abs(number) >= 1_000_000:
            return f"{number / 1_000_000:.{decimals}f}M"
        if abs(number) >= 10_000:
            return f"{number / 1_000:.{decimals}f}K"
        if number == int(number):
            return f"{int(number):,}"
        return f"{number:,.{decimals}f}"
    return str(value)


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("\n", " ")


def _numeric_or_text(value: Any) -> Any:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return _stringify(value)


def _sheet_name(title: str, index: int, used: set[str]) -> str:
    cleaned = "".join(ch for ch in title if ch not in "[]:*?/\\")[:26].strip() or f"Sheet{index}"
    candidate = cleaned
    suffix = 1
    while candidate in used:
        candidate = f"{cleaned[:24]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


RENDERERS: dict[ReportFormat, Renderer] = {
    ReportFormat.HTML: HtmlRenderer(),
    ReportFormat.MARKDOWN: MarkdownRenderer(),
    ReportFormat.JSON: JsonRenderer(),
    ReportFormat.CSV: CsvRenderer(),
    ReportFormat.XLSX: XlsxRenderer(),
    ReportFormat.PDF: PdfRenderer(),
}


def render(spec: ReportSpec, fmt: ReportFormat | str) -> tuple[bytes, str, str]:
    """Render a report. Returns ``(payload, extension, media_type)``."""
    report_format = ReportFormat(fmt)
    renderer = RENDERERS.get(report_format)
    if renderer is None:  # pragma: no cover - guarded by the enum
        raise UnsupportedOperationError(f"no renderer for format '{fmt}'")
    payload = renderer.render(spec)
    log.info("report_rendered", format=report_format.value, bytes=len(payload), title=spec.title)
    return payload, renderer.extension, renderer.media_type
