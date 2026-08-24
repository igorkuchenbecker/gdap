"""``gdap worker`` — run the job runner and the scheduler."""

from __future__ import annotations

from typing import Annotated

import typer

from gdap.cli.console import console, panel, success, table
from gdap.cli.main import platform, run_safely
from gdap.worker import JobRunner, Scheduler, WorkerConfig

app = typer.Typer(help="Worker: execute queued jobs and fire schedules.", no_args_is_help=True)


@app.command("start")
def start(
    concurrency: Annotated[int | None, typer.Option(help="parallel job slots")] = None,
    scheduler: Annotated[bool, typer.Option("--scheduler/--no-scheduler")] = True,
    worker_id: Annotated[
        str | None, typer.Option(help="identifier used in leases and logs")
    ] = None,
) -> None:
    """Run the worker until interrupted (Ctrl-C for a graceful stop)."""
    active = platform()
    active.bootstrap()
    settings = active.settings.worker
    config = WorkerConfig(
        worker_id=worker_id or f"worker-{active.settings.environment}",
        concurrency=concurrency or settings.concurrency,
        poll_interval_s=settings.poll_interval_s,
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        scheduler_enabled=scheduler and settings.scheduler_enabled,
    )
    runner = JobRunner(active, config)
    runner.install_signal_handlers()
    panel(
        "GDAP worker",
        f"id:          {config.worker_id}\n"
        f"concurrency: {config.concurrency}\n"
        f"scheduler:   {'on' if config.scheduler_enabled else 'off'}\n"
        f"database:    {active.db._safe_url()}",
    )
    processed = runner.run_forever()
    success(f"worker stopped after {processed} job(s)")


@app.command("drain")
def drain(
    max_jobs: Annotated[int, typer.Option(help="stop after this many jobs")] = 50,
) -> None:
    """Run every queued job, then exit (useful in CI and cron)."""
    active = platform()
    runner = JobRunner(active, WorkerConfig(worker_id="drain", concurrency=1, poll_interval_s=0.05))
    processed = run_safely(lambda: runner.drain(max_jobs=max_jobs))
    success(f"processed {processed} job(s)")


@app.command("schedule")
def schedule(
    tick: Annotated[bool, typer.Option("--tick", help="fire everything that is due now")] = False,
) -> None:
    """Show upcoming schedules, or fire the ones that are due."""
    active = platform()
    scheduler = Scheduler(active)
    if tick:
        queued = run_safely(scheduler.tick)
        success(f"{queued} job(s) queued")
        return
    upcoming = scheduler.upcoming(limit=20)
    if not upcoming:
        console.print("no schedules configured")
        return
    table(
        "Upcoming runs",
        ["pipeline", "next run (UTC)", "cron", "timezone", "last state"],
        [
            [
                row["pipeline"],
                (row["next_run_at"] or "")[:19],
                row["cron"] or "interval",
                row["timezone"],
                row["last_state"] or "—",
            ]
            for row in upcoming
        ],
    )
