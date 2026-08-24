"""Shared CLI rendering helpers.

Human-readable by default, ``--json`` everywhere for automation — the same command output can be
read by a person or piped into ``jq`` (§34).
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
error_console = Console(stderr=True)

STATE_STYLES = {
    "SUCCESS": "green",
    "pass": "green",
    "RUNNING": "cyan",
    "PENDING": "yellow",
    "RETRYING": "yellow",
    "warn": "yellow",
    "AWAITING_APPROVAL": "magenta",
    "FAILED": "red",
    "fail": "red",
    "CANCELLED": "dim",
    "critical": "red",
    "warning": "yellow",
    "info": "cyan",
}


def emit(payload: Any, *, as_json: bool) -> bool:
    """Print JSON when requested. Returns True when the caller should stop rendering."""
    if as_json:
        console.print_json(json.dumps(payload, default=str))
        return True
    return False


def style_state(value: str) -> str:
    return f"[{STATE_STYLES.get(value, 'white')}]{value}[/]"


def table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    rendered = Table(
        title=title, title_justify="left", header_style="bold", box=None, pad_edge=False
    )
    for column in columns:
        rendered.add_column(column, overflow="fold")
    for row in rows:
        rendered.add_row(*[("" if cell is None else str(cell)) for cell in row])
    console.print(rendered)
    console.print()


def panel(title: str, body: str, *, style: str = "cyan") -> None:
    console.print(Panel(body, title=title, border_style=style, expand=False))


def success(message: str) -> None:
    console.print(f"[green]✓[/] {message}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/] {message}")


def fail(message: str, *, code: str | None = None) -> None:
    error_console.print(f"[red]✗[/] {message}" + (f" [dim]({code})[/]" if code else ""))


def abort(message: str, *, code: str | None = None) -> None:
    fail(message, code=code)
    raise typer.Exit(code=1)


def truncate(value: Any, width: int = 60) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"
