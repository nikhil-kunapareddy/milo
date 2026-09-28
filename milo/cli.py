"""Milo's terminal interface: commands, prompts, and rendering. No business logic here."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from milo import __version__, config
from milo.backends.claude_code import CliStatus, check_cli
from milo.collectors import places, youtube
from milo.collectors.base import KeyCheck, KeyStatus

TAGLINE = "Milo · marketing research for food businesses"

app = typer.Typer(add_completion=False, invoke_without_command=True, help=TAGLINE)
console = Console(highlight=False)

KEY_CHECKS: dict[str, Callable[[str], Awaitable[KeyCheck]]] = {
    config.PLACES.name: places.check_key,
    config.YOUTUBE.name: youtube.check_key,
}

# name -> (what it adds, where to get a key, which API to enable)
KEY_HELP: dict[str, tuple[str, str, str]] = {
    config.PLACES.name: (
        "competitors, ratings, and reviews",
        "https://developers.google.com/maps/documentation/places/web-service/get-api-key",
        'Enable "Places API (New)" for the key\'s project.',
    ),
    config.YOUTUBE.name: (
        "local food videos and view counts",
        "https://developers.google.com/youtube/v3/getting-started",
        'Enable "YouTube Data API v3" for the key\'s project.',
    ),
}

STATUS_ICONS = {
    KeyStatus.VALID: "[green]✓[/]",
    KeyStatus.QUOTA: "[yellow]![/]",
    KeyStatus.UNREACHABLE: "[yellow]?[/]",
    KeyStatus.INVALID: "[red]✗[/]",
}


def _show_version(value: bool) -> None:
    if value:
        console.print(f"milo {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Milo · marketing research for food businesses."""
    if ctx.invoked_subcommand is None:
        console.print(f"[bold]{TAGLINE}[/]")
        console.print("Interactive research isn't built yet. Try `milo doctor` or `milo setup`.")


@app.command()
def setup() -> None:
    """Add optional Google Places and YouTube keys. Enter skips each one."""
    cfg = config.load()
    console.print(f"[bold]{TAGLINE}[/] · setup")
    console.print("Both keys are optional: without them Milo still researches the web.\n")
    if cfg.problem:
        console.print(f"[yellow]![/] Ignoring the current config file: {escape(cfg.problem)}\n")

    updates: dict[str, str] = {}
    for spec in config.KEYS:
        key = _ask_for_key(spec, cfg)
        if key:
            updates[spec.name] = key
        console.print()

    if not updates:
        console.print("No changes saved.")
        return
    path = config.save(updates)
    console.print(f"[green]✓[/] Saved to {_tilde(path)} (readable only by you). Run `milo doctor`.")


@app.command()
def doctor() -> None:
    """Show backend and key status. Keys are masked."""
    cfg = config.load()
    cli_status, checks = asyncio.run(_run_checks(cfg))

    console.print(f"[bold]{TAGLINE}[/] · doctor · v{__version__}\n")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Claude Code", _backend_line(cli_status))
    for spec in config.KEYS:
        grid.add_row(spec.label, _key_line(spec, cfg, checks.get(spec.name)))
    grid.add_row("Config", _config_line(cfg))
    console.print(grid)
    if not cli_status.ready:
        raise typer.Exit(1)


def ask_secret(label: str) -> str:
    """Hidden input, so pasted keys don't stay on screen. Tests replace this."""
    return Prompt.ask(label, password=True, default="", show_default=False, console=console)


def _ask_for_key(spec: config.KeySpec, cfg: config.Config) -> str | None:
    """Prompt until Google accepts the key or the user presses Enter."""
    what, url, enable = KEY_HELP[spec.name]
    current = cfg.key(spec)
    console.print(f"[bold]{spec.label}[/] · {what}")
    console.print(f"  Get a key: {url}")
    console.print(f"  {enable}")
    if current:
        console.print(f"  Current: {config.mask(current)} ({_source_label(spec, cfg)})")
    if cfg.source(spec) == "env":
        console.print(f"  [yellow]Note:[/] {spec.env_var} is set and overrides the saved key.")

    while True:
        key = ask_secret("  Paste key (Enter to skip)").strip()
        if not key:
            console.print("  [dim]– kept the current key[/]" if current else "  [dim]– skipped[/]")
            return None
        with console.status("  Checking with Google…"):
            result = asyncio.run(KEY_CHECKS[spec.name](key))
        masked = config.mask(key)
        if result.status is KeyStatus.VALID:
            console.print(f"  [green]✓[/] {masked} works")
            return key
        if result.status is KeyStatus.QUOTA:
            console.print(
                f"  [yellow]![/] Google accepted {masked}, but it's out of quota for now."
            )
            return key
        if result.status is KeyStatus.UNREACHABLE:
            console.print(f"  [yellow]?[/] Couldn't check {masked} ({escape(result.note)}).")
            if Confirm.ask("  Save it anyway?", default=False, console=console):
                return key
            continue
        console.print(f"  [red]✗[/] {escape(result.note)}. Try again, or press Enter to skip.")


async def _run_checks(cfg: config.Config) -> tuple[CliStatus, dict[str, KeyCheck]]:
    keys = {spec.name: key for spec in config.KEYS if (key := cfg.key(spec))}
    cli_status, *results = await asyncio.gather(
        check_cli(), *(KEY_CHECKS[name](key) for name, key in keys.items())
    )
    return cli_status, dict(zip(keys, results, strict=True))


def _backend_line(status: CliStatus) -> str:
    if status.ready:
        method = f" ({status.auth_method})" if status.auth_method else ""
        return f"[green]✓[/] {status.version or 'installed'} · logged in{method}"
    return f"[red]✗[/] {escape(status.problem or 'not available')}"


def _key_line(spec: config.KeySpec, cfg: config.Config, check: KeyCheck | None) -> str:
    key = cfg.key(spec)
    if not key or check is None:
        return "[dim]–[/] not configured · add one with `milo setup`"
    icon = STATUS_ICONS[check.status]
    return f"{icon} {escape(check.note)} · {config.mask(key)} ({_source_label(spec, cfg)})"


def _config_line(cfg: config.Config) -> str:
    path = config.config_path()
    shown = _tilde(path)
    if cfg.problem:
        return f"[red]✗[/] {escape(cfg.problem)} · `milo setup` will replace it"
    mode = config.file_mode(path)
    if mode is None:
        return f"[dim]–[/] {shown} (not created yet)"
    if not config.is_private(mode):
        return f"[yellow]![/] {shown} is readable by others ({mode:04o}) · fix: chmod 600 {shown}"
    return f"[green]✓[/] {shown} ({mode:04o})"


def _source_label(spec: config.KeySpec, cfg: config.Config) -> str:
    return f"from {spec.env_var}" if cfg.source(spec) == "env" else "config file"


def _tilde(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
