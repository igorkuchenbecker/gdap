"""``gdap dataset`` — catalog, preview, profile, validate, clean, query."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, style_state, success, table, truncate
from gdap.cli.main import run_safely, session
from gdap.core.services.dataset_service import DatasetService

app = typer.Typer(help="Datasets: catalog, profile, validate, clean, query.", no_args_is_help=True)


@app.command("list")
def list_datasets(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List datasets in the catalog."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            rows = context.datasets.list(limit=200)
            return [
                DatasetService.to_dict(row, latest=context.datasets.repo.latest_version(row.id))
                for row in rows
            ]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no datasets yet — ingest one with [bold]gdap source ingest[/]")
        return
    table(
        "Datasets",
        ["name", "rows", "version", "quality", "classification", "updated"],
        [
            [
                r["name"],
                f"{r['row_count']:,}",
                r["current_version"],
                f"{r['quality_score']:.1f}" if r["quality_score"] is not None else "—",
                r["classification"],
                r["updated_at"][:19],
            ]
            for r in rows
        ],
    )


@app.command("show")
def show(
    name: Annotated[str, typer.Argument()], as_json: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show a dataset with its schema and version history."""

    def operation() -> dict[str, Any]:
        with session() as context:
            row = context.datasets.get(name)
            latest = context.datasets.repo.latest_version(row.id)
            payload = DatasetService.to_dict(row, latest=latest)
            payload["schema"] = context.datasets.schema(name).model_dump(mode="json")
            payload["versions"] = [
                {"version": v.version, "rows": v.row_count, "created_at": v.created_at.isoformat()}
                for v in context.datasets.versions(name)[:10]
            ]
            return payload

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    table(
        f"Dataset '{payload['name']}'",
        ["property", "value"],
        [
            ["rows", f"{payload['row_count']:,}"],
            ["versions", payload["current_version"]],
            ["classification", payload["classification"]],
            ["quality", payload["quality_score"] or "not evaluated"],
            ["owner", payload["owner"] or "—"],
        ],
    )
    table(
        "Schema",
        ["column", "type", "meaning", "classification", "nullable"],
        [
            [c["name"], c["dtype"], c["semantic_type"], c["classification"], c["nullable"]]
            for c in payload["schema"]["columns"]
        ],
    )


@app.command("preview")
def preview(
    name: Annotated[str, typer.Argument()],
    rows: Annotated[int, typer.Option("--rows", "-n")] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Preview rows (sensitive columns are masked)."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.datasets.preview(name, rows=rows)

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    columns = [c["name"] for c in payload["columns"]][:10]
    table(
        f"{payload['dataset']} v{payload['version']} ({payload['rows_total']:,} rows)",
        columns,
        [[truncate(record.get(column), 22) for column in columns] for record in payload["records"]],
    )


@app.command("profile")
def profile(
    name: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Profile a dataset: distributions, missing values, keys, recommendations."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.datasets.profile(name).model_dump(mode="json", by_alias=True)

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    console.print(
        f"[bold]{payload['dataset']}[/]: {payload['rows']:,} rows × {payload['columns']} columns, "
        f"{payload['duplicate_rows']} duplicate row(s)\n"
    )
    table(
        "Columns",
        ["column", "type", "meaning", "missing", "distinct", "sample"],
        [
            [
                c["name"],
                c["dtype"],
                c["semantic_type"],
                f"{c['null_ratio'] * 100:.1f}%",
                c["distinct_count"],
                truncate(", ".join(str(v) for v in c["sample_values"][:2]), 28),
            ]
            for c in payload["column_profiles"]
        ],
    )
    if payload["recommendations"]:
        console.print("[bold]Recommendations[/]")
        for note in payload["recommendations"][:8]:
            console.print(f"  • {note}")


@app.command("validate")
def validate(
    name: Annotated[str, typer.Argument()],
    auto: Annotated[bool, typer.Option("--auto/--no-auto", help="derive expectations")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Score data quality across seven dimensions."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.datasets.validate(name, auto_expectations=auto).model_dump(mode="json")

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    console.print(
        f"[bold]{payload['dataset']}[/] quality: "
        f"{style_state(payload['status'])} {payload['score']:.1f}/100\n"
    )
    table(
        "Dimensions",
        ["dimension", "score", "weight", "failed checks"],
        [
            [d["dimension"], f"{d['score']:.1f}", f"{d['weight']:.2f}", d["failed"]]
            for d in payload["dimensions"]
        ],
    )
    if payload["findings"]:
        table(
            "Findings",
            ["severity", "rule", "column", "message"],
            [
                [
                    style_state(f["severity"]),
                    f["rule"],
                    f["column"] or "—",
                    truncate(f["message"], 60),
                ]
                for f in payload["findings"][:15]
            ],
        )


@app.command("clean")
def clean(
    name: Annotated[str, typer.Argument()],
    apply: Annotated[bool, typer.Option("--apply", help="apply the approved fixes")] = False,
    approve: Annotated[list[str], typer.Option("--approve", help="proposal id to approve")] = [],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Propose cleaning fixes; apply only what is approved."""

    def operation() -> dict[str, Any]:
        with session() as context:
            proposals, _ = context.datasets.propose_cleaning(name)
            payload: dict[str, Any] = {
                "proposals": [p.model_dump(mode="json") for p in proposals],
            }
            if apply:
                version, result = context.datasets.apply_cleaning(
                    name, proposals, approved_ids=set(approve)
                )
                payload["result"] = result.model_dump(mode="json")
                payload["new_version"] = version.version
            return payload

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    table(
        f"Cleaning proposals for '{name}'",
        ["id", "action", "column", "issue", "approval", "rows"],
        [
            [
                p["id"],
                p["action"],
                p["column"] or "—",
                truncate(p["issue"], 34),
                p["approval"],
                p["affected_rows"],
            ]
            for p in payload["proposals"]
        ],
    )
    if "result" in payload:
        result = payload["result"]
        success(
            f"applied {len(result['applied'])} fix(es); rows {result['rows_before']:,} → "
            f"{result['rows_after']:,} → new version v{payload['new_version']}"
        )
        for line in result["log"]:
            console.print(f"  • {line}")
    else:
        console.print("run again with [bold]--apply[/] (add --approve <id> for gated fixes)")


@app.command("query")
def query(
    sql: Annotated[str, typer.Argument(help="SELECT statement")],
    limit: Annotated[int, typer.Option()] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run guarded SQL across the workspace datasets."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.datasets.query(sql, limit=limit)

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    if not payload["records"]:
        console.print("no rows")
        return
    columns = payload["columns"][:12]
    table(
        f"{payload['rows']} row(s)",
        columns,
        [
            [truncate(record.get(column), 24) for column in columns]
            for record in payload["records"][:limit]
        ],
    )


@app.command("versions")
def versions(name: Annotated[str, typer.Argument()]) -> None:
    """List the immutable versions of a dataset."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [
                {
                    "version": v.version,
                    "rows": v.row_count,
                    "columns": v.column_count,
                    "size": v.size_bytes,
                    "checksum": v.checksum[:12],
                    "created": v.created_at.isoformat()[:19],
                }
                for v in context.datasets.versions(name)
            ]

    rows = run_safely(operation)
    table(
        f"Versions of '{name}'",
        ["version", "rows", "columns", "size (KiB)", "checksum", "created"],
        [
            [
                r["version"],
                f"{r['rows']:,}",
                r["columns"],
                f"{r['size'] / 1024:.1f}",
                r["checksum"],
                r["created"],
            ]
            for r in rows
        ],
    )
