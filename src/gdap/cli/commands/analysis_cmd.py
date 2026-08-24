"""``gdap analysis`` — run analyses and read insights."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, style_state, table, truncate
from gdap.cli.main import run_safely, session

app = typer.Typer(
    help="Analytics: describe, trend, anomaly, drivers, forecast.", no_args_is_help=True
)


@app.command("run")
def run(
    dataset: Annotated[str, typer.Argument()],
    kind: Annotated[
        str,
        typer.Argument(
            help="describe|trend|comparison|segmentation|drivers|anomaly|forecast|correlation"
        ),
    ],
    param: Annotated[list[str], typer.Option("--param", "-p", help="key=value (repeatable)")] = [],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run one analysis over a dataset."""
    params: dict[str, Any] = {}
    for item in param:
        key, _, value = item.partition("=")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.analyses.run(dataset, kind, params=params).model_dump(mode="json")

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    console.print(f"[bold]{payload['kind']}[/] — {payload['summary']}\n")
    for insight in payload["insights"]:
        console.print(f"  [{insight['kind']}] {insight['title']}")
        if insight["detail"]:
            console.print(f"      [dim]{truncate(insight['detail'], 100)}[/]")
    first_table: list[dict[str, Any]] = next(iter(payload["tables"].values()), [])
    if first_table:
        columns = list(first_table[0].keys())[:6]
        table(
            "Result",
            columns,
            [[truncate(row.get(column), 20) for column in columns] for row in first_table[:12]],
        )


@app.command("auto")
def auto(
    dataset: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run every analysis that applies to the dataset."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [result.model_dump(mode="json") for result in context.analyses.auto(dataset)]

    results = run_safely(operation)
    if emit(results, as_json=as_json):
        return
    for result in results:
        console.print(f"[bold]{result['kind']}[/]: {truncate(result['summary'], 100)}")
        for insight in result["insights"][:2]:
            console.print(f"    [{insight['kind']}] {truncate(insight['title'], 90)}")


@app.command("insights")
def insights(
    dataset: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option()] = 15,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the most important recent insights for a dataset."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [
                i.model_dump(mode="json") for i in context.analyses.insights(dataset, limit=limit)
            ]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no insights yet — run [bold]gdap analysis auto[/] first")
        return
    table(
        f"Insights for '{dataset}'",
        ["severity", "kind", "title", "confidence"],
        [
            [
                style_state(r["severity"]),
                r["kind"],
                truncate(r["title"], 62),
                f"{r['confidence']:.0%}",
            ]
            for r in rows
        ],
    )
