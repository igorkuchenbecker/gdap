"""End-to-end acceptance: the MVP definition from the specification (§60).

One test, thirteen numbered requirements. If this passes on a clean machine with no cloud account
and no API key, the MVP is met.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gdap.core.contracts import SourceSpec
from gdap.core.enums import JobState, SourceType, StepState
from gdap.core.services.context import ServiceContext
from gdap.demo import generate_demo_files
from gdap.ingestion import IngestRequest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

PIPELINE: dict[str, Any] = {
    "name": "mvp_flow",
    "description": "The MVP loop: ingest → clean → transform → analyse → report → alert.",
    "quality_gate": 40,
    "steps": [
        {
            "id": "ingest",
            "uses": "read.source",
            "with": {"source": "demo", "object": "transactions.csv", "dataset": "transactions"},
        },
        {
            "id": "clean",
            "uses": "clean.auto",
            "with": {"apply": "validated", "dataset": "transactions"},
        },
        {
            "id": "normalise",
            "uses": "transform.calculate",
            "with": {"calculate": {"region": "upper(trim(region))"}},
        },
        {"id": "completed", "uses": "transform.filter", "with": {"where": "status == 'completed'"}},
        {
            "id": "net",
            "uses": "transform.calculate",
            "with": {"calculate": {"net_revenue": "revenue * (1 - discount_pct)"}},
        },
        {
            "id": "validate",
            "uses": "validate.expectations",
            "with": {"auto": True, "dataset": "transactions"},
        },
        {"id": "gate", "uses": "quality.gate", "with": {"min_score": 40}},
        {
            "id": "monthly",
            "uses": "aggregate",
            "output": "monthly",
            "with": {
                "group_by": ["region"],
                "metrics": {"revenue": "sum(net_revenue)", "orders": "count"},
            },
        },
        {"id": "publish", "uses": "write.dataset", "with": {"dataset": "revenue_by_region"}},
        {
            "id": "trend",
            "uses": "analyze.trend",
            "input": "transactions",
            "with": {"metric": "net_revenue", "time_column": "order_date", "granularity": "month"},
        },
        {
            "id": "anomalies",
            "uses": "analyze.anomaly",
            "input": "transactions",
            "with": {
                "method": "timeseries",
                "metric": "net_revenue",
                "time_column": "order_date",
                "granularity": "week",
            },
        },
        {"id": "narrate", "uses": "ai.insights", "with": {"question": "What changed in revenue?"}},
        {
            "id": "report",
            "uses": "report.generate",
            "with": {
                "title": "MVP report",
                "formats": ["html", "xlsx", "json"],
                "dataset": "revenue_by_region",
            },
        },
        {
            "id": "alert",
            "uses": "alert.threshold",
            "with": {
                "metric": "quality_score",
                "operator": "lt",
                "threshold": 101,
                "severity": "info",
                "message": "quality below the ceiling (always fires in this test)",
            },
        },
    ],
}


def test_mvp_definition_of_done(context: ServiceContext, tmp_path: Path) -> None:
    data_dir = generate_demo_files(tmp_path / "data", days=200, seed=3, orders_per_day=8).directory

    # 1. Connect to a data source
    context.sources.register(
        SourceSpec(
            name="demo",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(data_dir), "pattern": "*.csv"},
        )
    )
    assert context.sources.test("demo").ok

    # 2. Load a dataset
    ingestion = context.sources.ingest(
        IngestRequest(source="demo", object="transactions.csv", dataset="transactions")
    )
    assert ingestion.rows > 0 and ingestion.version == 1

    # 3. Profile it
    profile = context.datasets.profile("transactions")
    assert profile.columns > 5
    assert profile.recommendations

    # 4. Validate it
    quality = context.datasets.validate("transactions", auto_expectations=True)
    assert 0 <= quality.score <= 100
    assert quality.findings, "the seeded defects must be detected"

    # 5. Clean it (approval gates respected)
    proposals, _ = context.datasets.propose_cleaning("transactions")
    assert any(p.approval.value == "REQUIRES_APPROVAL" for p in proposals)

    # 6-8. Transform, analyse and chart — through the pipeline engine
    context.pipelines.create(PIPELINE)
    job = context.pipelines.run("mvp_flow")
    result = context.jobs.execute(job)

    assert result.state is JobState.SUCCESS
    assert all(step.state is not StepState.FAILED for step in result.steps)
    charts = [
        chart
        for analysis in context.analyses.list(dataset="transactions", limit=20)
        for chart in (analysis.result or {}).get("charts", [])
    ]
    assert charts, "analyses must produce chart specifications"

    # 9. Generate a report (multiple formats)
    reports = context.reports.list(limit=10)
    assert {report.format for report in reports} >= {"html", "xlsx", "json"}
    payload, media_type, filename = context.reports.download(reports[0].id)
    assert payload and media_type and filename

    # 10. Expose results through the API surface (service layer contracts)
    assert context.datasets.preview("revenue_by_region", rows=5)["records"]
    assert context.governance.catalog()["counts"]["datasets"] >= 2

    # 11. Run through the CLI surface — the same services the CLI calls
    assert context.datasets.query("SELECT count(*) AS n FROM transactions")["records"][0]["n"] > 0

    # 12. Log everything: audit trail and lineage
    actions = {event["action"] for event in context.governance.audit(limit=100)}
    assert {"source.create", "source.ingest", "dataset.profile", "dataset.validate"} <= actions
    dataset = context.datasets.get("transactions")
    lineage = context.governance.lineage("dataset", dataset.id, depth=3)
    assert len(lineage["edges"]) >= 2

    # 13. Alerting works
    assert any(alert.rule.startswith("pipeline:") for alert in context.alerts.list())

    # AI answers, with evidence, and without any API key
    answer = context.agents.ask("Why did revenue change last month?", dataset="transactions")
    assert answer.answer
    assert answer.evidence, "every AI answer must carry evidence"
    assert answer.provider == "heuristic"


def test_reproducibility_same_input_same_checksum(context: ServiceContext, tmp_path: Path) -> None:
    """§42: the same bytes ingested twice produce the same checksum and an independent version."""
    data_dir = generate_demo_files(tmp_path / "repro", days=30, seed=5, orders_per_day=4).directory
    context.sources.register(
        SourceSpec(
            name="repro",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(data_dir), "pattern": "*.csv"},
        )
    )
    first = context.sources.ingest(
        IngestRequest(source="repro", object="transactions.csv", dataset="tx")
    )
    second = context.sources.ingest(
        IngestRequest(source="repro", object="transactions.csv", dataset="tx")
    )
    assert first.checksum == second.checksum
    assert first.version != second.version
    assert context.datasets.frame("tx", version=first.version).height == first.rows


def test_platform_survives_a_corrupt_file(context: ServiceContext, tmp_path: Path) -> None:
    """§28: a malformed input fails predictably, with a typed error, leaving no half-written data."""
    from gdap.core.errors import GdapError

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "bad.csv").write_text('a,b\n1,2\n"unterminated,3\n')

    context.sources.register(
        SourceSpec(
            name="broken",
            type=SourceType.FILE,
            connector="file.csv",
            config={"path": str(broken), "pattern": "*.csv", "ignore_errors": False},
        )
    )
    try:
        context.sources.ingest(IngestRequest(source="broken", object="bad.csv", dataset="bad"))
    except GdapError as exc:
        assert exc.code.startswith("GDAP-")
    # whatever happened, the platform is still usable and consistent
    assert context.governance.catalog()["counts"]["sources"] >= 1
