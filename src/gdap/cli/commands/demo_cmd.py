"""``gdap demo`` — the end-to-end demonstration scenario (§61).

One command takes a clean machine from *nothing* to: data generated, ingested, versioned,
profiled, validated, cleaned, transformed, analysed, explained and reported — with lineage and an
audit trail behind every step. It uses no cloud account and no API key.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from gdap.cli.console import console, panel, style_state, success, table, truncate
from gdap.cli.main import platform, run_safely, session
from gdap.core.contracts import SourceSpec
from gdap.core.enums import SourceType
from gdap.demo import generate_demo_files
from gdap.ingestion import IngestRequest

app = typer.Typer(help="Run the built-in demonstration scenario.", no_args_is_help=True)

DEMO_PIPELINE: dict[str, Any] = {
    "name": "demo_sales_daily",
    "description": "Clean transactions, compute monthly revenue by region, analyse and report.",
    "tags": ["demo"],
    "params": {"min_quality": 60},
    "quality_gate": 50,
    "steps": [
        {
            "id": "ingest",
            "uses": "read.source",
            "with": {
                "source": "demo_files",
                "object": "transactions.csv",
                "dataset": "transactions",
            },
        },
        {
            "id": "clean",
            "uses": "clean.auto",
            "with": {"apply": "validated", "dataset": "transactions"},
        },
        {
            "id": "normalise_region",
            "uses": "transform.calculate",
            "with": {"calculate": {"region": "upper(trim(region))"}},
        },
        {
            "id": "completed_only",
            "uses": "transform.filter",
            "with": {"where": "status == 'completed'"},
        },
        {
            "id": "net_revenue",
            "uses": "transform.calculate",
            "with": {"calculate": {"net_revenue": "revenue * (1 - discount_pct)"}},
        },
        {
            "id": "validate",
            "uses": "validate.expectations",
            "with": {"auto": True, "dataset": "transactions"},
        },
        {"id": "gate", "uses": "quality.gate", "with": {"min_score": "${params.min_quality}"}},
        {
            "id": "calendar",
            "uses": "enrich.datetime",
            "with": {"column": "order_date", "parts": ["year", "month"]},
        },
        {
            "id": "monthly",
            "uses": "aggregate",
            "output": "monthly",
            "with": {
                "group_by": ["region", "order_date_year", "order_date_month"],
                "metrics": {
                    "revenue": "sum(net_revenue)",
                    "orders": "count",
                    "avg_ticket": "mean(net_revenue)",
                },
            },
        },
        {"id": "publish", "uses": "write.dataset", "with": {"dataset": "sales_monthly"}},
        {
            "id": "trend",
            "uses": "analyze.trend",
            "input": "transactions",
            "with": {"metric": "net_revenue", "time_column": "order_date", "granularity": "month"},
        },
        {
            "id": "by_region",
            "uses": "analyze.segmentation",
            "input": "transactions",
            "with": {"metric": "net_revenue", "dimension": "region"},
        },
        {
            "id": "month_over_month",
            "uses": "analyze.comparison",
            "input": "transactions",
            "with": {
                "metric": "net_revenue",
                "time_column": "order_date",
                "dimension": "region",
                "granularity": "month",
            },
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
        {
            "id": "drivers",
            "uses": "analyze.drivers",
            "input": "transactions",
            "with": {"metric": "net_revenue"},
        },
        {
            "id": "narrate",
            "uses": "ai.insights",
            "with": {"question": "What changed in revenue and why?"},
        },
        {
            "id": "report",
            "uses": "report.generate",
            "with": {
                "title": "Sales review",
                "formats": ["html", "xlsx"],
                "dataset": "sales_monthly",
            },
        },
        {
            "id": "quality_alert",
            "uses": "alert.threshold",
            "with": {
                "metric": "quality_score",
                "operator": "lt",
                "threshold": 85,
                "severity": "warning",
                "message": "Transaction data quality is below the agreed threshold.",
            },
        },
    ],
}


@app.command("run")
def run(
    days: Annotated[int, typer.Option(help="days of history to generate")] = 540,
    keep: Annotated[bool, typer.Option("--keep/--fresh", help="reuse existing demo data")] = True,
    ask: Annotated[bool, typer.Option("--ask/--no-ask", help="finish with AI questions")] = True,
) -> None:
    """Generate data and run the full platform loop end to end."""
    active = platform()
    started = time.perf_counter()
    run_safely(active.bootstrap)

    data_dir = Path(active.settings.paths.home) / "demo-data"
    step = _stepper()

    step("Generating a realistic dataset with deliberate defects")
    if keep and (data_dir / "transactions.csv").exists():
        console.print(f"  reusing {data_dir}")
        stats: dict[str, Any] = {"note": "existing files reused"}
    else:
        dataset = run_safely(lambda: generate_demo_files(data_dir, days=days))
        stats = dataset.stats
        defects = stats["seeded_defects"]
        table(
            "Generated",
            ["file", "rows"],
            [[name, stats[name]] for name in ("customers", "products", "regions", "transactions")],
        )
        console.print(
            "  seeded defects: "
            f"{defects['missing_revenue']} missing revenues, {defects['duplicate_rows']} duplicates, "
            f"{defects['case_variant_regions']} category variants, {defects['orphan_customers']} orphan keys, "
            f"a revenue collapse in {defects['revenue_shock_region']}\n"
        )

    step("Registering the source and ingesting four datasets")

    def ingest_all() -> list[dict[str, Any]]:
        rows = []
        with session() as context:
            if not context.sources.repo.by_name("demo_files"):
                context.sources.register(
                    SourceSpec(
                        name="demo_files",
                        type=SourceType.FILE,
                        connector="file.csv",
                        config={"path": str(data_dir), "pattern": "*.csv"},
                        description="Demonstration CSV extracts",
                        tags=["demo"],
                    )
                )
            for file_name, dataset_name in (
                ("transactions.csv", "transactions"),
                ("customers.csv", "customers"),
                ("products.csv", "products"),
                ("regions.csv", "regions"),
            ):
                result = context.sources.ingest(
                    IngestRequest(source="demo_files", object=file_name, dataset=dataset_name)
                )
                rows.append(
                    {
                        "dataset": result.dataset,
                        "rows": result.rows,
                        "version": result.version,
                        "bytes": result.bytes_written,
                        "checksum": result.checksum[:12],
                    }
                )
        return rows

    ingested = run_safely(ingest_all)
    table(
        "Ingested",
        ["dataset", "rows", "version", "size (KiB)", "checksum"],
        [
            [
                r["dataset"],
                f"{r['rows']:,}",
                r["version"],
                f"{r['bytes'] / 1024:.1f}",
                r["checksum"],
            ]
            for r in ingested
        ],
    )

    step("Profiling and validating the raw transactions")

    def inspect() -> dict[str, Any]:
        with session() as context:
            profile = context.datasets.profile("transactions")
            quality = context.datasets.validate("transactions", auto_expectations=True)
            proposals, _ = context.datasets.propose_cleaning("transactions")
            return {
                "profile": profile.model_dump(mode="json", by_alias=True),
                "quality": quality.model_dump(mode="json"),
                "proposals": [p.model_dump(mode="json") for p in proposals],
            }

    inspection = run_safely(inspect)
    quality = inspection["quality"]
    console.print(
        f"  quality {style_state(quality['status'])} {quality['score']:.1f}/100 · "
        f"{inspection['profile']['duplicate_rows']} duplicate rows detected\n"
    )
    table(
        "What the platform found on its own",
        ["severity", "rule", "message"],
        [
            [style_state(f["severity"]), f["rule"], truncate(f["message"], 62)]
            for f in quality["findings"][:6]
        ],
    )
    table(
        "Cleaning proposals (approval gates included)",
        ["id", "action", "column", "approval", "rows"],
        [
            [p["id"], p["action"], p["column"] or "—", p["approval"], p["affected_rows"]]
            for p in inspection["proposals"]
        ],
    )

    step("Running the automated pipeline")

    def run_pipeline() -> dict[str, Any]:
        with session() as context:
            if context.pipelines.repo.by_name(DEMO_PIPELINE["name"]):
                context.pipelines.update(DEMO_PIPELINE["name"], DEMO_PIPELINE)
            else:
                context.pipelines.create(DEMO_PIPELINE)
            job = context.pipelines.run(DEMO_PIPELINE["name"])
            return context.jobs.execute(job).model_dump(mode="json")

    result = run_safely(run_pipeline)
    table(
        f"Pipeline '{result['pipeline']}' → {result['state']}",
        ["step", "uses", "state", "rows out", "detail"],
        [
            [
                s["step_id"],
                s["uses"],
                style_state(s["state"]),
                s["rows_out"] if s["rows_out"] is not None else "—",
                truncate((s["metrics"] or {}).get("message", s.get("error") or ""), 46),
            ]
            for s in result["steps"]
        ],
    )

    if result["insights"]:
        console.print("[bold]Insights produced by the run[/]")
        for insight in result["insights"][:8]:
            console.print(f"  [{insight['kind']}] {truncate(insight['title'], 92)}")
        console.print()

    if result["artifacts"]:
        console.print("[bold]Artifacts[/]")
        for uri in result["artifacts"]:
            console.print(f"  {uri}")
        console.print()

    if ask:
        step("Asking the AI Data Analyst (no API key required)")

        def interrogate() -> list[dict[str, Any]]:
            answers = []
            with session() as context:
                for question in (
                    "What is the revenue trend?",
                    "Why did revenue change last month?",
                    "Where are the anomalies in revenue?",
                ):
                    answer = context.agents.ask(question, dataset="transactions")
                    answers.append({"question": question, **answer.model_dump(mode="json")})
            return answers

        for answer in run_safely(interrogate):
            panel(answer["question"], answer["answer"], style="cyan")

    step("Governance trail")

    def governance() -> dict[str, Any]:
        with session() as context:
            catalog = context.governance.catalog()
            audit = context.governance.audit(limit=8)
            dataset = context.datasets.get("transactions")
            lineage = context.governance.lineage("dataset", dataset.id, depth=3)
            alerts = [
                {"severity": a.severity, "title": a.title} for a in context.alerts.list(limit=5)
            ]
            return {"catalog": catalog, "audit": audit, "lineage": lineage, "alerts": alerts}

    trail = run_safely(governance)
    table(
        "Catalog",
        ["dataset", "rows", "versions", "quality", "classification"],
        [
            [
                entry["dataset"],
                f"{entry['rows']:,}",
                entry["versions"],
                f"{entry['quality_score']:.1f}" if entry["quality_score"] else "—",
                entry["classification"],
            ]
            for entry in trail["catalog"]["datasets"]
        ],
    )
    table(
        "Audit trail (most recent)",
        ["action", "resource", "actor", "result"],
        [
            [e["action"], e["resource_type"], truncate(e["actor"], 22), e["result"]]
            for e in trail["audit"]
        ],
    )
    console.print(
        f"lineage around 'transactions': {len(trail['lineage']['nodes'])} node(s), "
        f"{len(trail['lineage']['edges'])} edge(s)"
    )
    if trail["alerts"]:
        for alert in trail["alerts"]:
            console.print(f"  [{alert['severity']}] {truncate(alert['title'], 80)}")

    elapsed = time.perf_counter() - started
    panel(
        "Demo complete",
        f"Everything above ran locally in {elapsed:.1f}s — no cloud account, no API key.\n\n"
        f"Explore it:\n"
        f"  gdap dataset list\n"
        f"  gdap dataset validate transactions\n"
        f'  gdap agent ask "why did revenue fall?" --dataset transactions\n'
        f"  gdap pipeline run {DEMO_PIPELINE['name']}\n"
        f"  gdap system serve      # API + web UI on http://127.0.0.1:8000",
        style="green",
    )


@app.command("reset")
def reset(
    yes: Annotated[bool, typer.Option("--yes", help="skip confirmation")] = False,
) -> None:
    """Delete the demo workspace (database, warehouse, artifacts and generated data)."""
    active = platform()
    home = Path(active.settings.paths.home)
    if not yes and not typer.confirm(f"delete everything under {home}?"):
        raise typer.Abort()
    import shutil

    active.shutdown()
    if home.exists():
        shutil.rmtree(home)
    success(f"removed {home}")


def _stepper() -> Any:
    counter = {"n": 0}

    def step(title: str) -> None:
        counter["n"] += 1
        console.rule(f"[bold cyan]{counter['n']}. {title}")

    return step
