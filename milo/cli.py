"""Milo's terminal interface: commands, prompts, and rendering. No business logic here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from milo import __version__, config
from milo import collectors as sources
from milo.backends import BACKENDS, DEFAULT_BACKEND, get_backend
from milo.backends.base import Backend, BackendStatus
from milo.backends.claude_code import check_cli
from milo.backends.codex import CodexBackend
from milo.collectors import places, youtube
from milo.collectors.base import KeyCheck, KeyStatus
from milo.models import Session, SessionState
from milo.session import Ask, Controller, Say, Step, UIEvent, Working
from milo.store import Store

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

TONES = {"info": None, "ok": "green", "warn": "yellow", "error": "red", "muted": "dim"}
STEP_ICONS = {"ok": "[green]✓[/]", "skip": "[dim]–[/]", "work": "[cyan]→[/]", "fail": "[red]✗[/]"}

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
    backend: Annotated[
        str, typer.Option("--backend", help=f"Agent backend: {', '.join(BACKENDS)}.")
    ] = DEFAULT_BACKEND,
) -> None:
    """Milo · marketing research for food businesses."""
    if backend not in BACKENDS:
        raise typer.BadParameter(f"choose one of: {', '.join(BACKENDS)}", param_hint="--backend")
    ctx.obj = {"backend": backend}
    if ctx.invoked_subcommand is None:
        raise typer.Exit(_interactive(backend))


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
    grid.add_row("Codex", _codex_line())
    for spec in config.KEYS:
        grid.add_row(spec.label, _key_line(spec, cfg, checks.get(spec.name)))
    grid.add_row("Config", _config_line(cfg))
    console.print(grid)
    if not cli_status.ready:
        raise typer.Exit(1)


# Interactive session -----------------------------------------------------------------


def _interactive(backend_name: str, session: Session | None = None) -> int:
    backend = get_backend(backend_name)
    cfg = config.load()
    status = asyncio.run(backend.check())
    console.print(f"[bold]{TAGLINE}[/]")
    console.print(_status_line(backend, status, cfg))
    if not status.ready:
        console.print(f"[red]✗[/] {escape(status.problem or 'The backend is not available.')}")
        return 1
    store = Store()
    controller = Controller(
        backend=backend, collectors=sources.build(cfg), store=store, session=session
    )
    _repl(controller)
    return 0


def _repl(controller: Controller) -> None:
    for event in controller.opening():
        _render(event)
    while controller.state is not SessionState.ENDED:
        try:
            text = console.input("[bold]>[/] ")
        except (EOFError, KeyboardInterrupt):  # Ctrl-D or Ctrl-C at the prompt: save and quit
            console.print()
            text = "/exit"
        try:
            asyncio.run(_render_stream(controller.handle(text)))
        except KeyboardInterrupt:  # Ctrl-C mid-run: the backend is stopped, the session kept
            console.print(f"\n[yellow]Stopped.[/] {escape(controller.hint())}")


async def _render_stream(events: AsyncIterator[UIEvent]) -> None:
    spinner = None
    try:
        async with aclosing(events) as stream:
            async for event in stream:
                if isinstance(event, Working):
                    if spinner is None:
                        spinner = console.status(escape(event.text))
                        spinner.start()
                    else:
                        spinner.update(escape(event.text))
                    continue
                _render(event)
    finally:
        if spinner is not None:
            spinner.stop()


def _render(event: UIEvent) -> None:
    if isinstance(event, Say):
        console.print(escape(event.text), style=TONES[event.tone])
    elif isinstance(event, Ask):
        console.print(f"\n[bold]{escape(event.text)}[/]")
    elif isinstance(event, Step):
        console.print(f"{STEP_ICONS[event.status]} {escape(event.text)}")


def _status_line(backend: Backend, status: BackendStatus, cfg: config.Config) -> str:
    mark = "[green]✓[/]" if status.ready else "[red]✗[/]"
    parts = [f"Backend: {backend.label} {mark}"]
    for spec in config.KEYS:
        configured = "[green]✓[/]" if cfg.key(spec) else "[dim]– (not configured)[/]"
        parts.append(f"{spec.label} {configured}")
    return " · ".join(parts)


# Setup and doctor helpers --------------------------------------------------------------


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


async def _run_checks(cfg: config.Config) -> tuple[BackendStatus, dict[str, KeyCheck]]:
    keys = {spec.name: key for spec in config.KEYS if (key := cfg.key(spec))}
    cli_status, *results = await asyncio.gather(
        check_cli(), *(KEY_CHECKS[name](key) for name, key in keys.items())
    )
    return cli_status, dict(zip(keys, results, strict=True))


def _backend_line(status: BackendStatus) -> str:
    if status.ready:
        method = f" ({status.auth_method})" if status.auth_method else ""
        return f"[green]✓[/] {status.version or 'installed'} · logged in{method}"
    return f"[red]✗[/] {escape(status.problem or 'not available')}"


def _codex_line() -> str:
    if CodexBackend().available():
        return "[dim]–[/] found, but not supported yet · Milo uses Claude Code for now"
    return "[dim]–[/] not installed (optional, not supported yet)"


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
