"""``gdap job`` — monitor and control runs."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, style_state, success, table, truncate
from gdap.cli.main import run_safely, session

app = typer.Typer(help="Jobs: monitor, approve, cancel, retry.", no_args_is_help=True)


@app.command("list")
def list_jobs(
    state: Annotated[str | None, typer.Option(help="filter by state")] = None,
    pipeline: Annotated[str | None, typer.Option(help="filter by pipeline")] = None,
    limit: Annotated[int, typer.Option()] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List recent jobs."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [
                context.jobs.to_dict(row)
                for row in context.jobs.list(limit=limit, state=state, pipeline=pipeline)
            ]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no jobs yet")
        return
    table(
        "Jobs",
        ["id", "pipeline", "state", "trigger", "attempt", "duration", "created"],
        [
            [
                r["id"][:8],
                r["pipeline"],
                style_state(r["state"]),
                r["trigger"],
                f"{r['attempt']}/{r['max_attempts']}",
                f"{(r['metrics'] or {}).get('duration_seconds', 0):.1f}s",
                r["created_at"][:19],
            ]
            for r in rows
        ],
    )


@app.command("show")
def show(
    job_id: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a job with its steps, metrics and artifacts."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.to_dict(context.jobs.get(job_id), include_steps=True)

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    console.print(
        f"[bold]{payload['pipeline']}[/] {style_state(payload['state'])} "
        f"(attempt {payload['attempt']}/{payload['max_attempts']}, trace {payload['trace_id']})\n"
    )
    table(
        "Steps",
        ["#", "step", "uses", "state", "rows out", "detail"],
        [
            [
                step["index"],
                step["step_id"],
                step["uses"],
                style_state(step["state"]),
                step["rows_out"] if step["rows_out"] is not None else "—",
                truncate((step["metrics"] or {}).get("message", step.get("error") or ""), 50),
            ]
            for step in payload.get("steps", [])
        ],
    )
    if payload["error"]:
        console.print(f"[red]error:[/] {payload['error']} [dim]({payload['error_code']})[/]")
    if payload["approval_request"].get("steps"):
        console.print(
            f"[magenta]awaiting approval[/] for: {', '.join(payload['approval_request']['steps'])}\n"
            f"approve with: [bold]gdap job approve {payload['id']}[/]"
        )


@app.command("approve")
def approve(
    job_id: Annotated[str, typer.Argument()],
    step: Annotated[list[str], typer.Option("--step", help="approve only these steps")] = [],
    note: Annotated[str | None, typer.Option(help="reason recorded in the audit trail")] = None,
) -> None:
    """Approve blocked steps and re-queue the job."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.to_dict(context.jobs.approve(job_id, steps=step or None, note=note))

    payload = run_safely(operation)
    success(f"job {payload['id'][:8]} approved and re-queued ({payload['state']})")


@app.command("reject")
def reject(
    job_id: Annotated[str, typer.Argument()], reason: Annotated[str, typer.Option(...)]
) -> None:
    """Reject a pending approval and cancel the job."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.to_dict(context.jobs.reject(job_id, reason=reason))

    payload = run_safely(operation)
    success(f"job {payload['id'][:8]} rejected")


@app.command("cancel")
def cancel(
    job_id: Annotated[str, typer.Argument()], reason: Annotated[str | None, typer.Option()] = None
) -> None:
    """Cancel a job."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.to_dict(context.jobs.cancel(job_id, reason=reason))

    payload = run_safely(operation)
    success(f"job {payload['id'][:8]} cancelled")


@app.command("retry")
def retry(job_id: Annotated[str, typer.Argument()]) -> None:
    """Re-queue a failed job."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.to_dict(context.jobs.retry(job_id))

    payload = run_safely(operation)
    success(f"job {payload['id'][:8]} re-queued")


@app.command("run")
def run_now(job_id: Annotated[str, typer.Argument()]) -> None:
    """Execute a queued job in this process (no worker needed)."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return context.jobs.execute(context.jobs.get(job_id)).model_dump(mode="json")

    payload = run_safely(operation)
    success(f"job finished: {payload['state']}")
    if payload["state"] != "SUCCESS":
        raise typer.Exit(code=1)
