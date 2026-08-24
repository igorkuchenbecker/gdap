"""``gdap report`` — generate and download report artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, success, table, truncate
from gdap.cli.main import run_safely, session
from gdap.core.services.report_service import ReportService

app = typer.Typer(help="Reports: create, list, download.", no_args_is_help=True)


@app.command("create")
def create(
    dataset: Annotated[str, typer.Argument()],
    title: Annotated[str | None, typer.Option()] = None,
    fmt: Annotated[
        list[str], typer.Option("--format", "-f", help="html|xlsx|csv|json|markdown|pdf")
    ] = ["html"],
    open_after: Annotated[
        bool, typer.Option("--open", help="open the HTML report in a browser")
    ] = False,
) -> None:
    """Profile, validate, analyse and render a report for a dataset."""

    def operation() -> dict[str, Any]:
        with session() as context:
            reports, spec = context.reports.dataset_report(dataset, title=title, formats=list(fmt))
            return {
                "items": [ReportService.to_dict(row) for row in reports],
                "executive_summary": spec.executive_summary,
                "paths": [row.storage_uri for row in reports],
            }

    payload = run_safely(operation)
    for item, uri in zip(payload["items"], payload["paths"], strict=True):
        success(f"{item['format']}: {uri} ({item['size_bytes'] / 1024:.1f} KiB)")
    if payload["executive_summary"]:
        console.print("\n[bold]Executive summary[/]")
        console.print(payload["executive_summary"])
    if open_after:
        import webbrowser

        for uri in payload["paths"]:
            if uri.endswith(".html"):
                webbrowser.open(uri)
                break


@app.command("list")
def list_reports(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List report artifacts."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [ReportService.to_dict(row) for row in context.reports.list(limit=100)]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no reports yet")
        return
    table(
        "Reports",
        ["id", "name", "format", "size (KiB)", "dataset", "created"],
        [
            [
                r["id"][:8],
                truncate(r["name"], 34),
                r["format"],
                f"{r['size_bytes'] / 1024:.1f}",
                r["dataset"] or "—",
                r["created_at"][:19],
            ]
            for r in rows
        ],
    )


@app.command("download")
def download(
    report_id: Annotated[str, typer.Argument()],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="destination path")] = None,
) -> None:
    """Download a report artifact."""

    def operation() -> tuple[bytes, str, str]:
        with session() as context:
            return context.reports.download(report_id)

    payload, _media_type, filename = run_safely(operation)
    destination = output or Path(filename)
    destination.write_bytes(payload)
    success(f"saved {len(payload) / 1024:.1f} KiB to {destination}")
