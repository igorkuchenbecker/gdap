"""``gdap pipeline`` — author, schedule and run pipelines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gdap.cli.console import console, emit, style_state, success, table, truncate
from gdap.cli.main import run_safely, session
from gdap.core.services.pipeline_service import PipelineService
from gdap.pipelines.spec import load_spec

app = typer.Typer(help="Pipelines: create, schedule, run, inspect.", no_args_is_help=True)


@app.command("create")
def create(
    file: Annotated[Path, typer.Argument(help="pipeline YAML file")],
    update: Annotated[
        bool, typer.Option("--update", help="publish a new version if it exists")
    ] = False,
) -> None:
    """Create (or update) a pipeline from a YAML specification."""

    def operation() -> dict[str, Any]:
        spec = load_spec(file)
        with session() as context:
            existing = context.pipelines.repo.by_name(spec.name)
            if existing and update:
                return PipelineService.to_dict(context.pipelines.update(spec.name, spec))
            return PipelineService.to_dict(context.pipelines.create(spec))

    row = run_safely(operation)
    success(
        f"pipeline '{row['name']}' v{row['version']} with {row['step_count']} step(s)"
        + (f" — next run {row['next_run_at']}" if row["next_run_at"] else "")
    )


@app.command("list")
def list_pipelines(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List pipelines."""

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            return [PipelineService.to_dict(row) for row in context.pipelines.list(limit=200)]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("no pipelines yet — see [bold]examples/pipelines/[/]")
        return
    table(
        "Pipelines",
        ["name", "v", "steps", "enabled", "schedule", "next run", "last state"],
        [
            [
                r["name"],
                r["version"],
                r["step_count"],
                "yes" if r["enabled"] else "no",
                (r["schedule"] or {}).get("cron") or (r["schedule"] or {}).get("every") or "manual",
                (r["next_run_at"] or "—")[:19],
                style_state(r["last_state"]) if r["last_state"] else "—",
            ]
            for r in rows
        ],
    )


@app.command("show")
def show(
    name: Annotated[str, typer.Argument()], as_json: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show a pipeline and its steps."""

    def operation() -> dict[str, Any]:
        with session() as context:
            row = context.pipelines.get(name)
            payload = PipelineService.to_dict(row)
            payload["spec"] = row.spec
            payload["yaml"] = context.pipelines.as_yaml(name)
            return payload

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    console.print(
        f"[bold]{payload['name']}[/] v{payload['version']} — {payload['description'] or ''}\n"
    )
    table(
        "Steps",
        ["#", "id", "uses", "with"],
        [
            [
                index + 1,
                step.get("id") or "",
                step.get("uses"),
                truncate(json.dumps(step.get("with", {})), 58),
            ]
            for index, step in enumerate(payload["spec"].get("steps", []))
        ],
    )


@app.command("run")
def run(
    name: Annotated[str, typer.Argument()],
    param: Annotated[list[str], typer.Option("--param", "-p", help="key=value (repeatable)")] = [],
    wait: Annotated[
        bool, typer.Option("--wait/--queue", help="run inline or queue for a worker")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a pipeline now."""
    params: dict[str, Any] = {}
    for item in param:
        key, _, value = item.partition("=")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value

    def operation() -> dict[str, Any]:
        with session() as context:
            job = context.pipelines.run(name, params=params)
            if not wait:
                return {"job_id": job.id, "state": job.state, "queued": True}
            result = context.jobs.execute(job)
            return result.model_dump(mode="json")

    payload = run_safely(operation)
    if emit(payload, as_json=as_json):
        return
    if payload.get("queued"):
        success(f"job {payload['job_id']} queued — start a worker with [bold]gdap worker start[/]")
        return

    console.print(
        f"\n[bold]{payload['pipeline']}[/] → {style_state(payload['state'])} "
        f"in {payload['metrics'].get('duration_seconds', 0):.2f}s (job {payload['job_id'][:8]})\n"
    )
    table(
        "Steps",
        ["step", "uses", "state", "rows in", "rows out", "detail"],
        [
            [
                step["step_id"],
                step["uses"],
                style_state(step["state"]),
                step["rows_in"] if step["rows_in"] is not None else "—",
                step["rows_out"] if step["rows_out"] is not None else "—",
                truncate(step["metrics"].get("message", step.get("error") or ""), 52),
            ]
            for step in payload["steps"]
        ],
    )
    if payload.get("insights"):
        console.print("[bold]Insights[/]")
        for insight in payload["insights"][:6]:
            console.print(f"  [{insight['kind']}] {truncate(insight['title'], 90)}")
    if payload.get("artifacts"):
        console.print("\n[bold]Artifacts[/]")
        for uri in payload["artifacts"]:
            console.print(f"  {uri}")
    if payload["state"] != "SUCCESS":
        raise typer.Exit(code=1)


@app.command("enable")
def enable(
    name: Annotated[str, typer.Argument()],
    disable: Annotated[bool, typer.Option("--disable")] = False,
) -> None:
    """Enable or disable a pipeline's schedule."""

    def operation() -> dict[str, Any]:
        with session() as context:
            return PipelineService.to_dict(context.pipelines.set_enabled(name, not disable))

    row = run_safely(operation)
    success(f"pipeline '{row['name']}' {'disabled' if disable else 'enabled'}")


@app.command("delete")
def delete(
    name: Annotated[str, typer.Argument()], yes: Annotated[bool, typer.Option("--yes")] = False
) -> None:
    """Delete a pipeline definition (its run history is kept)."""
    if not yes and not typer.confirm(f"delete pipeline '{name}'?"):
        raise typer.Abort()

    def operation() -> None:
        with session() as context:
            context.pipelines.delete(name)

    run_safely(operation)
    success(f"pipeline '{name}' deleted")


@app.command("steps")
def steps(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the step types available to pipelines."""
    from gdap.pipelines.steps import step_catalog

    catalogue = step_catalog()
    if emit(catalogue, as_json=as_json):
        return
    table(
        "Pipeline steps",
        ["key", "category", "read-only", "approval", "description"],
        [
            [
                s["key"],
                s["category"],
                "yes" if s["read_only"] else "no",
                s["approval"],
                truncate(s["description"], 52),
            ]
            for s in catalogue
        ],
    )


@app.command("validate")
def validate(file: Annotated[Path, typer.Argument(help="pipeline YAML file")]) -> None:
    """Validate a pipeline file without creating it."""

    def operation() -> Any:
        return load_spec(file)

    spec = run_safely(operation)
    success(f"'{spec.name}' is valid — {len(spec.steps)} step(s), fingerprint {spec.fingerprint()}")


@app.command("from-text")
def from_text(
    request: Annotated[str, typer.Argument(help="what you want the pipeline to do")],
    dataset: Annotated[str | None, typer.Option("--dataset", "-d")] = None,
    create: Annotated[bool, typer.Option("--create", help="store the generated pipeline")] = False,
) -> None:
    """Generate a reviewable pipeline from a natural-language request (§41)."""

    def operation() -> dict[str, Any]:
        with session() as context:
            plan = context.agents.plan(request, dataset=dataset)
            payload = plan.model_dump(mode="json", by_alias=True)
            if create:
                payload["created"] = PipelineService.to_dict(context.pipelines.create(plan.spec))
            return payload

    payload = run_safely(operation)
    plan_spec = payload["spec"]
    console.print(f"[bold]{plan_spec['name']}[/] — {payload['rationale']}\n")
    table(
        "Proposed steps",
        ["#", "id", "uses", "with"],
        [
            [i + 1, s.get("id") or "", s["uses"], truncate(json.dumps(s.get("with", {})), 54)]
            for i, s in enumerate(plan_spec["steps"])
        ],
    )
    if payload["assumptions"]:
        console.print("[bold]Assumptions[/]")
        for assumption in payload["assumptions"]:
            console.print(f"  • {assumption}")
    if payload.get("created"):
        success(f"pipeline '{payload['created']['name']}' created — review it before running")
    else:
        console.print("\nrun again with [bold]--create[/] to store this pipeline")
