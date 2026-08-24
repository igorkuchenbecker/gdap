"""``gdap source`` — register, probe, discover and ingest."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from gdap.cli.console import abort, console, emit, success, table, truncate
from gdap.cli.main import run_safely, session
from gdap.core.contracts import SourceSpec
from gdap.core.enums import IngestionMode, SourceType
from gdap.core.services.source_service import SourceService
from gdap.ingestion import IngestRequest

app = typer.Typer(help="Data sources: register, test, discover, ingest.", no_args_is_help=True)


@app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="unique source name")],
    connector: Annotated[str, typer.Option("--connector", "-c", help="e.g. file.csv, sql, rest")],
    config: Annotated[list[str], typer.Option("--set", help="config key=value (repeatable)")] = [],
    secret: Annotated[list[str], typer.Option("--secret", help="name=env:VAR (repeatable)")] = [],
    description: Annotated[str | None, typer.Option()] = None,
    tag: Annotated[list[str], typer.Option("--tag")] = [],
) -> None:
    """Register a source. Secrets are referenced, never stored: --secret password=env:PGPASS."""
    settings_map = _pairs(config)
    secret_map = _pairs(secret)
    source_type = connector.split(".")[0]
    if source_type not in {member.value for member in SourceType}:
        source_type = "file" if connector.startswith("file") else source_type

    def operation() -> dict[str, Any]:
        with session() as context:
            spec = SourceSpec(
                name=name,
                type=source_type,  # type: ignore[arg-type]
                connector=connector,
                config=settings_map,
                secret_refs=secret_map,
                description=description,
                tags=list(tag),
            )
            return SourceService.to_dict(context.sources.register(spec))

    row = run_safely(operation)
    success(f"source '{row['name']}' registered ({row['connector']})")


@app.command("list")
def list_sources(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List registered sources."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [SourceService.to_dict(row) for row in context.sources.list(limit=200)]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no sources yet — try [bold]gdap source add --help[/]")
        return
    table(
        "Sources",
        ["name", "connector", "status", "classification", "last tested"],
        [
            [
                r["name"],
                r["connector"],
                r["status"],
                r["classification"],
                r["last_tested_at"] or "never",
            ]
            for r in rows
        ],
    )


@app.command("test")
def test(name: Annotated[str, typer.Argument()]) -> None:
    """Probe connectivity and permissions without moving data."""

    def operation() -> Any:
        with session() as context:
            return context.sources.test(name)

    result = run_safely(operation)
    if result.ok:
        success(f"{result.message} ({result.latency_ms:.0f}ms)")
    else:
        abort(result.message)


@app.command("discover")
def discover(
    name: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the objects (files, tables, endpoints) a source exposes."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [
                obj.model_dump(mode="json", by_alias=True) for obj in context.sources.discover(name)
            ]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    table(
        f"Objects in '{name}'",
        ["object", "kind", "format", "rows", "size"],
        [
            [
                r["name"],
                r["kind"],
                r.get("format") or "",
                r.get("estimated_rows") or "",
                r.get("size_bytes") or "",
            ]
            for r in rows
        ],
    )


@app.command("ingest")
def ingest(
    name: Annotated[str, typer.Argument(help="source name")],
    object_: Annotated[
        str | None, typer.Option("--object", "-o", help="file/table to read")
    ] = None,
    dataset: Annotated[str | None, typer.Option("--dataset", "-d", help="target dataset")] = None,
    mode: Annotated[str, typer.Option(help="full|incremental|append")] = "full",
    incremental_column: Annotated[str | None, typer.Option(help="high-water-mark column")] = None,
    dedupe: Annotated[list[str], typer.Option("--dedupe-key")] = [],
    limit: Annotated[int | None, typer.Option(help="max rows")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ingest data into a versioned dataset."""

    def operation() -> dict[str, Any]:
        with session() as context:
            result = context.sources.ingest(
                IngestRequest(
                    source=name,
                    object=object_,
                    dataset=dataset,
                    mode=IngestionMode(mode),
                    incremental_column=incremental_column,
                    dedupe_keys=list(dedupe),
                    limit=limit,
                )
            )
            return result.model_dump(mode="json", by_alias=True)

    result = run_safely(operation)
    if emit(result, as_json=as_json):
        return
    success(
        f"{result['rows']:,} rows → dataset '{result['dataset']}' v{result['version']} "
        f"({result['bytes_written'] / 1024:.1f} KiB, checksum {result['checksum'][:12]})"
    )
    if result.get("warnings"):
        for warning in result["warnings"]:
            console.print(f"  [yellow]![/] {truncate(warning, 100)}")


@app.command("rm")
def remove(
    name: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", help="skip confirmation")] = False,
) -> None:
    """Delete a source (datasets it produced are kept)."""
    if not yes and not typer.confirm(f"delete source '{name}'?"):
        raise typer.Abort()

    def operation() -> None:
        with session() as context:
            context.sources.delete(name)

    run_safely(operation)
    success(f"source '{name}' deleted")


def _pairs(values: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            abort(f"expected key=value, got '{item}'")
        key, _, value = item.partition("=")
        try:  # allow JSON values: --set options='{"a":1}'
            parsed[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            parsed[key.strip()] = value
    return parsed
