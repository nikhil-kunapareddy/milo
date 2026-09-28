"""Milo's terminal interface: commands, prompts, and rendering. No business logic here."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
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
from milo.models import Brief, Session, SessionState, Source
from milo.session import (
    Ask,
    Controller,
    Say,
    ShowBrief,
    ShowSources,
    ShowText,
    Step,
    UIEvent,
    Working,
)
from milo.store import SessionNotFound, Store

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
        str | None,
        typer.Option(
            "--backend", help=f"Agent backend: {', '.join(BACKENDS)} (default {DEFAULT_BACKEND})."
        ),
    ] = None,
) -> None:
    """Milo · marketing research for food businesses."""
    if backend is not None and backend not in BACKENDS:
        raise typer.BadParameter(f"choose one of: {', '.join(BACKENDS)}", param_hint="--backend")
    ctx.obj = {"backend": backend}
    if ctx.invoked_subcommand is None:
        raise typer.Exit(_interactive(backend or DEFAULT_BACKEND))


@app.command()
def resume(
    ctx: typer.Context,
    session_id: Annotated[
        str | None, typer.Argument(help="Session to reopen. Default: the most recent.")
    ] = None,
) -> None:
    """Reopen a saved session and keep asking follow-ups."""
    store = Store()
    try:
        session = store.load(session_id) if session_id else store.latest()
    except SessionNotFound as exc:
        console.print(f"[red]✗[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    if session is None:
        console.print("No saved sessions yet. Run `milo` to start one.")
        raise typer.Exit(1)
    backend = ctx.obj.get("backend") or (
        session.backend if session.backend in BACKENDS else DEFAULT_BACKEND
    )
    raise typer.Exit(_interactive(backend, session=session))


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
    elif isinstance(event, ShowBrief):
        _render_brief(event.brief)
    elif isinstance(event, ShowText):
        console.print()
        console.print(Panel(Markdown(event.text), title=event.title or None, title_align="left"))
    elif isinstance(event, ShowSources):
        render_sources(event.sources)


def _render_brief(brief: Brief) -> None:
    console.print()
    console.print(
        Panel(
            escape(brief.market_snapshot),
            title="[bold]Market snapshot[/]",
            title_align="left",
            border_style="cyan",
        )
    )
    if brief.competitors:
        # Numbers only ever come from Google Places; without it, skip three empty columns.
        numbers = any(
            c.rating is not None or c.review_count is not None or c.price_level
            for c in brief.competitors
        )
        table = _table("Competitors")
        table.add_column("Name", style="bold", ratio=2)
        if numbers:
            table.add_column("Rating", justify="right", no_wrap=True)
            table.add_column("Reviews", justify="right", no_wrap=True)
            table.add_column("Price", no_wrap=True)
        table.add_column("Positioning", ratio=4)
        table.add_column("Sources", style="dim", no_wrap=True)
        for c in brief.competitors:
            stats = (
                [
                    f"{c.rating:.1f}" if c.rating is not None else "–",
                    f"{c.review_count:,}" if c.review_count is not None else "–",
                    escape(c.price_level or "–"),
                ]
                if numbers
                else []
            )
            table.add_row(escape(c.name), *stats, escape(c.positioning), _refs(c.source_ids))
        console.print(table)
        if not numbers:
            note = "Ratings, review counts, and prices need a Google Places key: `milo setup`."
            console.print(note, style="dim")
    _bullets("Review themes", brief.review_themes)
    if brief.content_benchmarks:
        _bullets("Content benchmarks", brief.content_benchmarks)
    _bullets("Gaps and opportunities", brief.gaps)

    _heading("Campaign ideas")
    for number, idea in enumerate(brief.campaign_ideas, 1):
        console.print(f"[bold]{number}. {escape(idea.title)}[/] [dim]{_refs(idea.source_ids)}[/]")
        console.print(f"   {escape(idea.idea)}")
        console.print(f"   [dim]Why it fits:[/] {escape(idea.why_it_fits)}")

    calendar = _table("7-day content calendar")
    calendar.add_column("Day", style="bold", no_wrap=True)
    calendar.add_column("Platform", no_wrap=True)
    calendar.add_column("Post")
    for day in brief.content_calendar:
        calendar.add_row(escape(day.day), escape(day.platform), escape(day.post))
    console.print()
    console.print(calendar)
    render_sources(brief.sources)


def render_sources(sources: list[Source]) -> None:
    _heading("Sources")
    if not sources:
        console.print("[dim]No verified sources yet.[/]")
    for source in sources:
        console.print(f"[dim]{source.id:>2}.[/] {escape(source.title)}")
        console.print(f"    [blue]{escape(source.url)}[/]")


def _table(title: str) -> Table:
    return Table(title=title, title_justify="left", title_style="bold", expand=True)


def _heading(title: str) -> None:
    console.print(f"\n[bold]{title}[/]")


def _bullets(title: str, items: list[str]) -> None:
    _heading(title)
    if not items:
        console.print("[dim]  None found.[/]")
    for item in items:
        console.print(f"  • {escape(item)}")


def _refs(ids: list[int]) -> str:
    return escape("".join(f"[{i}]" for i in ids))


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
