"""``gdap`` — the command line interface.

The CLI drives the *service layer* in-process rather than going over HTTP: it is an operator tool
that must work before an API server exists (bootstrap, diagnostics, recovery). It exposes the same
capabilities as the API, so anything scriptable in the UI is scriptable here (§34).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from gdap import __version__
from gdap.cli.console import abort, console, emit, panel, style_state, success, table
from gdap.core.config import PathsSettings, Settings, reset_settings
from gdap.core.container import Platform, get_platform, reset_platform
from gdap.core.errors import GdapError
from gdap.core.services.context import ServiceContext

app = typer.Typer(
    name="gdap",
    help="GDAP — Global Data Automation Platform.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)

_STATE: dict[str, Any] = {"home": None, "org": None}


def _settings() -> Settings:
    reset_settings()
    home = _STATE.get("home")
    if home:
        return Settings(paths=PathsSettings(home=Path(home)))
    return Settings()


def platform() -> Platform:
    return get_platform(_settings())


@contextmanager
def session() -> Iterator[ServiceContext]:
    """One transaction, acting as the organisation's owner."""
    active = platform()
    with active.db.session() as db_session:
        principal = active.resolve_principal(db_session, org_slug=_STATE.get("org"))
        yield active.context(db_session, principal)


def run_safely(operation: Any) -> Any:
    """Turn a domain error into a clean CLI failure instead of a traceback."""
    try:
        return operation()
    except GdapError as exc:
        details = ""
        if exc.details:
            details = " " + ", ".join(f"{k}={v}" for k, v in list(exc.details.items())[:3])
        abort(f"{exc.message}{details}", code=exc.code)
    except KeyboardInterrupt:  # pragma: no cover
        abort("interrupted")


@app.callback()
def main(
    home: Annotated[
        Path | None, typer.Option("--home", help="GDAP data directory (default ~/.gdap)")
    ] = None,
    org: Annotated[str | None, typer.Option("--org", help="organisation slug")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="debug logging")] = False,
) -> None:
    if home:
        _STATE["home"] = home
    if org:
        _STATE["org"] = org
    if verbose:
        import os

        os.environ["GDAP_OBSERVABILITY__LOG_LEVEL"] = "DEBUG"
    reset_platform()


@app.command()
def version() -> None:
    """Show the platform version."""
    console.print(f"GDAP {__version__}")


# ──────────────────────────────────────────── system ───────────────────────────────────────

system_app = typer.Typer(help="Initialise, inspect and operate the platform.", no_args_is_help=True)
app.add_typer(system_app, name="system")


@system_app.command("init")
def system_init(
    org: Annotated[str, typer.Option(help="organisation slug")] = "default",
    name: Annotated[str | None, typer.Option(help="organisation display name")] = None,
) -> None:
    """Create the database schema and the default organisation."""
    active = platform()
    principal = run_safely(lambda: active.bootstrap(org_slug=org, org_name=name))
    success(f"platform ready at {active.settings.paths.home}")
    table(
        "Workspace",
        ["setting", "value"],
        [
            ["organisation", org],
            ["owner", principal.email or principal.user_id],
            ["database", active.db._safe_url()],
            ["warehouse", str(active.settings.paths.warehouse)],
            ["environment", active.settings.environment],
        ],
    )
    console.print("Next: [bold]gdap demo run[/] or [bold]gdap source add --help[/]")


@system_app.command("health")
def system_health(
    deep: Annotated[bool, typer.Option("--deep", help="run the full diagnostic")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="machine-readable output")] = False,
) -> None:
    """Check that the platform's subsystems are reachable."""
    from gdap.observability.health import check_platform

    result = run_safely(lambda: check_platform(platform(), deep=deep))
    if emit(result.model_dump(mode="json"), as_json=as_json):
        raise typer.Exit(code=0 if result.ok else 1)
    table(
        f"Health — {'OK' if result.ok else 'DEGRADED'} (v{result.version}, {result.environment})",
        ["component", "status", "latency", "message"],
        [
            [
                check.component,
                "[green]ok[/]" if check.ok else "[red]failed[/]",
                f"{check.latency_ms:.1f}ms" if check.latency_ms else "",
                check.message,
            ]
            for check in result.checks
        ],
    )
    raise typer.Exit(code=0 if result.ok else 1)


@system_app.command("doctor")
def system_doctor(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Full self-diagnostic: database, storage, connectors, engine, AI runtime, scheduler."""
    system_health(deep=True, as_json=as_json)


@system_app.command("info")
def system_info(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show configuration and capabilities."""
    from gdap.pipelines.steps import known_steps

    active = platform()
    settings = active.settings
    payload = {
        "version": __version__,
        "environment": settings.environment,
        "home": str(settings.paths.home),
        "database": active.db._safe_url(),
        "auth_enabled": settings.security.auth_enabled,
        "ai_provider": settings.ai.provider,
        "connectors": active.registry.keys(),
        "steps": known_steps(),
        "locale": settings.locale.model_dump(mode="json"),
    }
    if emit(payload, as_json=as_json):
        return
    table(
        "Platform",
        ["setting", "value"],
        [
            ["version", __version__],
            ["environment", settings.environment],
            ["home", str(settings.paths.home)],
            ["database", active.db._safe_url()],
            ["auth", "enabled" if settings.security.auth_enabled else "disabled (non-production)"],
            ["AI provider", settings.ai.provider],
            ["connectors", ", ".join(active.registry.keys())],
            ["pipeline steps", str(len(known_steps()))],
        ],
    )


@system_app.command("serve")
def system_serve(
    host: Annotated[str | None, typer.Option(help="bind address")] = None,
    port: Annotated[int | None, typer.Option(help="port")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="auto-reload (development)")] = False,
    workers: Annotated[int, typer.Option(help="uvicorn workers")] = 1,
) -> None:
    """Run the HTTP API (and the bundled web UI)."""
    import uvicorn

    active = platform()
    active.bootstrap()
    settings = active.settings
    bind_host = host or settings.api.host
    bind_port = port or settings.api.port
    panel(
        "GDAP API",
        f"http://{bind_host}:{bind_port}\n"
        f"docs:   http://{bind_host}:{bind_port}/docs\n"
        f"health: http://{bind_host}:{bind_port}/health\n"
        f"auth:   {'API key required' if settings.security.auth_enabled else 'disabled (non-production)'}",
    )
    uvicorn.run(
        "gdap.api.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        workers=workers if not reload else 1,
        log_config=None,
    )


key_app = typer.Typer(help="Manage API keys.", no_args_is_help=True)
system_app.add_typer(key_app, name="key")


@key_app.command("create")
def key_create(
    name: Annotated[str, typer.Argument(help="a label for the key")],
    role: Annotated[str, typer.Option(help="owner|admin|engineer|analyst|viewer")] = "analyst",
    expires_in_days: Annotated[int | None, typer.Option(help="optional expiry")] = None,
) -> None:
    """Issue an API key. The secret is shown once and never stored in clear text."""
    from datetime import UTC, datetime, timedelta

    from gdap.core.enums import Role
    from gdap.security import api_keys
    from gdap.security.rbac import permissions_for
    from gdap.storage.repositories import ApiKeyRepository, UserRepository

    def operation() -> dict[str, Any]:
        with session() as context:
            users = UserRepository(context.session, context.org_id)
            email = f"{name}@service.local"
            user = users.by_email(email) or users.create(email=email, name=name, role=role)
            issued = api_keys.generate()
            expires_at = (
                datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
            )
            record = ApiKeyRepository(context.session, context.org_id).create(
                user_id=user.id,
                name=name,
                prefix=issued.prefix,
                key_hash=issued.key_hash,
                scopes=[p.value for p in permissions_for(Role(role))],
                expires_at=expires_at,
            )
            context.audit.record(
                context.principal, "apikey.create", "api_key", record.id, details={"name": name}
            )
            return {"id": record.id, "name": name, "role": role, "key": issued.plaintext}

    result = run_safely(operation)
    panel(
        "API key created — copy it now",
        f"{result['key']}\n\nrole: {result['role']}   id: {result['id']}",
        style="yellow",
    )


@key_app.command("list")
def key_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List issued API keys (secrets are never shown)."""
    from gdap.storage.repositories import ApiKeyRepository

    def operation() -> list[dict[str, Any]]:
        with session() as context:
            rows = ApiKeyRepository(context.session, context.org_id).list(limit=200)
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "prefix": row.prefix,
                    "created_at": row.created_at.isoformat(),
                    "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                    "revoked": row.revoked_at is not None,
                }
                for row in rows
            ]

    rows = run_safely(operation)
    if emit(rows, as_json=as_json):
        return
    table(
        "API keys",
        ["id", "name", "prefix", "last used", "revoked"],
        [
            [r["id"][:8], r["name"], r["prefix"], r["last_used_at"] or "never", r["revoked"]]
            for r in rows
        ],
    )


@key_app.command("revoke")
def key_revoke(key_id: Annotated[str, typer.Argument(help="key id")]) -> None:
    """Revoke an API key immediately."""
    from gdap.storage.repositories import ApiKeyRepository

    def operation() -> None:
        with session() as context:
            ApiKeyRepository(context.session, context.org_id).revoke(key_id)
            context.audit.record(context.principal, "apikey.revoke", "api_key", key_id)

    run_safely(operation)
    success(f"key {key_id} revoked")


def register_commands() -> None:
    """Attach the command groups (kept in submodules to keep each file readable)."""
    from gdap.cli.commands import (
        agent_cmd,
        analysis_cmd,
        dataset_cmd,
        demo_cmd,
        job_cmd,
        pipeline_cmd,
        report_cmd,
        source_cmd,
        worker_cmd,
    )

    app.add_typer(source_cmd.app, name="source")
    app.add_typer(dataset_cmd.app, name="dataset")
    app.add_typer(pipeline_cmd.app, name="pipeline")
    app.add_typer(job_cmd.app, name="job")
    app.add_typer(analysis_cmd.app, name="analysis")
    app.add_typer(report_cmd.app, name="report")
    app.add_typer(agent_cmd.app, name="agent")
    app.add_typer(worker_cmd.app, name="worker")
    app.add_typer(demo_cmd.app, name="demo")


register_commands()


def run() -> None:  # pragma: no cover - entry point
    app()


__all__ = ["app", "run", "session", "platform", "run_safely", "style_state"]
