"""Report renderers: every format the platform advertises must actually render."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gdap.core.contracts import ChartSpec, Insight, ReportSection, ReportSpec
from gdap.core.enums import InsightKind
from gdap.core.errors import UnsupportedOperationError
from gdap.reporting.renderers import (
    CsvRenderer,
    JsonRenderer,
    MarkdownRenderer,
    PdfRenderer,
    XlsxRenderer,
    _format_number,
    _sheet_name,
    _stringify,
    render,
)


def _spec(**overrides: object) -> ReportSpec:
    defaults: dict[str, object] = {
        "title": "Sales review",
        "subtitle": "Automated analysis",
        "executive_summary": "Revenue is up.",
        "kpis": [
            {"label": "Revenue", "value": 12345.678, "unit": "$", "change_pct": 5.2},
            {"label": "Orders", "value": 42, "change_pct": None},
        ],
        "sections": [
            ReportSection(
                title="Trend",
                body="Revenue over time.",
                table=[
                    {"month": "2026-01", "revenue": 1000.5},
                    {"month": "2026-02", "revenue": None},
                ],
                charts=[ChartSpec(kind="line", title="Revenue", x="month", y="revenue", data=[])],
                insights=[
                    Insight(kind=InsightKind.INFERENCE, title="Growing", detail="Steady climb.")
                ],
            )
        ],
        "methodology": "Computed with a linear regression over monthly totals.",
        "generated_at": datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return ReportSpec(**defaults)


def test_csv_renderer_flattens_sections_and_kpis() -> None:
    payload = CsvRenderer().render(_spec())
    text = payload.decode("utf-8")
    assert "Sales review" in text
    assert "Revenue" in text
    assert "section: Trend" in text
    assert "2026-01" in text


def test_markdown_renderer_includes_kpis_insights_and_table() -> None:
    payload = MarkdownRenderer().render(_spec())
    text = payload.decode("utf-8")
    assert text.startswith("# Sales review")
    assert "## Key indicators" in text
    assert "+5.2%" in text
    assert "—" in text  # kpi with no change_pct renders as em dash
    assert "Growing" in text
    assert "| month | revenue |" in text


def test_json_renderer_round_trips_the_spec() -> None:
    import json

    payload = JsonRenderer().render(_spec())
    data = json.loads(payload)
    assert data["title"] == "Sales review"
    assert data["sections"][0]["title"] == "Trend"


def test_xlsx_renderer_produces_a_valid_workbook() -> None:
    payload = XlsxRenderer().render(_spec())
    assert payload[:2] == b"PK"  # xlsx is a zip archive


def test_pdf_renderer_fails_with_actionable_message_when_weasyprint_missing() -> None:
    with pytest.raises(UnsupportedOperationError) as exc_info:
        PdfRenderer().render(_spec())

    error = exc_info.value
    assert error.code == "GDAP-2003"
    assert "weasyprint" in error.message.lower()
    assert error.details["format"] == "pdf"
    assert "pdf" not in error.details["available"]
    assert "html" in error.details["available"]


def test_render_dispatches_by_format_string() -> None:
    payload, extension, media_type = render(_spec(), "markdown")
    assert extension == "md"
    assert media_type == "text/markdown"
    assert payload.startswith(b"# Sales review")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (True, "yes"),
        (False, "no"),
        (1_500_000_000, "1.50B"),
        (2_500_000, "2.50M"),
        (15_000, "15.00K"),
        (42, "42"),
        (3.14159, "3.14"),
        ("plain", "plain"),
    ],
)
def test_format_number_covers_every_branch(value: object, expected: str) -> None:
    assert _format_number(value) == expected


def test_stringify_collapses_newlines_and_formats_floats() -> None:
    assert _stringify(None) == ""
    assert _stringify(3.0) == "3"
    assert _stringify("a\nb") == "a b"


def test_sheet_name_strips_illegal_characters_and_dedupes() -> None:
    used: set[str] = set()
    first = _sheet_name("Q1: Sales/Report [final]", 0, used)
    second = _sheet_name("Q1: Sales/Report [final]", 1, used)
    assert first == "Q1 SalesReport final"
    assert second == first + "_1"
    assert first in used and second in used


def test_sheet_name_falls_back_to_index_when_title_is_all_illegal_characters() -> None:
    used: set[str] = set()
    assert _sheet_name("[]:*?/\\", 3, used) == "Sheet3"
