"""``gdap agent`` — the AI Data Analyst from the terminal."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, panel, table, truncate
from gdap.cli.main import run_safely, session

app = typer.Typer(help="AI: ask questions, plan pipelines, inspect tools.", no_args_is_help=True)


@app.command("ask")
def ask(
    question: Annotated[str, typer.Argument(help="a question about your data")],
    dataset: Annotated[str | None, typer.Option("--dataset", "-d")] = None,
    agent: Annotated[str | None, typer.Option(help="force a specific agent")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ask the AI Data Analyst. Every claim comes with its evidence."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.agents.ask(question, dataset=dataset, agent=agent).model_dump(
                mode="json"
            )

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return

    panel(question, payload["answer"], style="cyan")
    if payload["insights"]:
        table(
            "Insights",
            ["kind", "severity", "title", "confidence"],
            [
                [i["kind"], i["severity"], truncate(i["title"], 58), f"{i['confidence']:.0%}"]
                for i in payload["insights"][:8]
            ],
        )
    if payload["evidence"]:
        table(
            "Evidence",
            ["source", "calculation / query", "rows"],
            [
                [
                    e["source"],
                    truncate(e.get("calculation") or e.get("query") or "", 56),
                    e.get("rows_considered") or "—",
                ]
                for e in payload["evidence"][:8]
            ],
        )
    console.print(
        f"[dim]tools: {', '.join(call['tool'] for call in payload['tool_calls']) or 'none'} · "
        f"provider: {payload['provider']} · confidence: {payload['confidence']:.0%}[/]"
    )
    for limitation in payload["limitations"]:
        console.print(f"[yellow]![/] {limitation}")


@app.command("plan")
def plan(
    request: Annotated[str, typer.Argument(help="describe the pipeline you want")],
    dataset: Annotated[str | None, typer.Option("--dataset", "-d")] = None,
    create: Annotated[bool, typer.Option("--create", help="store the pipeline for review")] = False,
) -> None:
    """Turn a natural-language request into a reviewable pipeline (§41)."""
    from gdap.cli.commands.pipeline_cmd import from_text

    from_text(request=request, dataset=dataset, create=create)


@app.command("tools")
def tools(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the tools agents may use, with their permissions and approval gates."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return {
                "tools": context.agents.tools(),
                "agents": context.agents.agents(),
                "status": context.agents.status(),
            }

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    status = payload["status"]
    console.print(
        f"AI mode: [bold]{status['mode']}[/] (provider {status['provider']}"
        + (f", model {status['model']}" if status["model"] else "")
        + ")\n"
    )
    table(
        "Tools",
        ["tool", "category", "read-only", "approval", "description"],
        [
            [
                t["name"],
                t["category"],
                "yes" if t["read_only"] else "no",
                t["approval"],
                truncate(t["description"], 46),
            ]
            for t in payload["tools"]
        ],
    )
    table(
        "Agents",
        ["agent", "tools", "description"],
        [[a["name"], len(a["tools"]), truncate(a["description"], 60)] for a in payload["agents"]],
    )
