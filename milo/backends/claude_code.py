"""Claude Code backend: the user's own `claude` CLI, run headless.

This module and stream.py are the only code that knows Claude Code's flags and output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from milo.backends.base import BackendEvent, Error, RunOptions
from milo.backends.stream import LOGIN_HINT, WEB_TOOLS, StreamParser

INSTALL_HINT = (
    "Install it with `curl -fsSL https://claude.ai/install.sh | bash` "
    "(more options: https://code.claude.com/docs/en/setup)."
)
STREAM_LIMIT = 32 * 1024 * 1024  # one stream-json line can hold a whole fetched page


@dataclass(frozen=True)
class CliStatus:
    installed: bool
    logged_in: bool | None = None  # None when the CLI couldn't tell us
    version: str | None = None
    auth_method: str | None = None
    problem: str | None = None  # what's wrong and how to fix it

    @property
    def ready(self) -> bool:
        return self.installed and self.logged_in is True


class ClaudeCodeBackend:
    name = "claude-code"
    label = "Claude Code"

    def __init__(self, executable: str = "claude") -> None:
        self._executable = executable

    def available(self) -> bool:
        return shutil.which(self._executable) is not None

    async def check(self) -> CliStatus:
        return await check_cli(self._executable)

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        resume: str | None = None,
        options: RunOptions | None = None,
    ) -> AsyncIterator[BackendEvent]:
        """Stream events from one `claude -p` turn. Never raises for expected failures."""
        path = shutil.which(self._executable)
        if path is None:
            yield Error("not_installed", f"Claude Code isn't installed. {INSTALL_HINT}")
            return
        cwd.mkdir(parents=True, exist_ok=True)
        command = build_command(path, resume=resume, options=options or RunOptions())
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=_child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                start_new_session=True,  # its own process group, so we can stop all of it
            )
        except OSError as exc:
            yield Error("crashed", f"Couldn't start Claude Code ({exc.strerror or exc}).")
            return

        assert proc.stdin and proc.stdout and proc.stderr
        stderr = asyncio.create_task(proc.stderr.read())
        parser = StreamParser()
        try:
            # The prompt goes over stdin: no argument-length limit, and it stays out of `ps`.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            try:
                async for line in proc.stdout:
                    for event in parser.feed(line.decode(errors="replace")):
                        yield event
            except ValueError:  # a line longer than STREAM_LIMIT
                yield Error("crashed", "Claude Code sent more output than Milo can read.")
                return
            returncode = await proc.wait()
            # A leftover child can hold stderr open after the CLI exits; don't wait on it.
            done, _ = await asyncio.wait({stderr}, timeout=2.0)
            err_text = stderr.result().decode(errors="replace") if done else ""
            for event in parser.finish(returncode, err_text):
                yield event
        finally:
            if proc.returncode is None:  # the caller stopped early (Ctrl-C, error)
                await _stop(proc)
            if not stderr.done():
                stderr.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr


def build_command(executable: str, *, resume: str | None, options: RunOptions) -> list[str]:
    tools = ",".join(WEB_TOOLS) if options.web else ""
    command = [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",  # required for stream-json with -p
        "--safe-mode",  # no user hooks, plugins, CLAUDE.md, or MCP servers; login still works
        "--permission-prompts",
        "none",  # anything not explicitly allowed is denied instead of waiting on a prompt
        "--tools",
        tools,  # the only tools that exist in the session ("" means none)
        "--max-turns",
        str(options.max_turns),
    ]
    if options.web:
        command += ["--allowedTools", tools]
    if options.schema is not None:
        command += ["--json-schema", json.dumps(options.schema)]
    if options.ephemeral:
        command.append("--no-session-persistence")
    if resume:
        command += ["--resume", resume]
    return command


async def _stop(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """End the CLI and anything it started. SIGTERM first so it can save the session."""
    _signal_group(proc, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + grace
    # Poll the exit code rather than wait(): wait() also waits for every pipe to close,
    # and a stray child can keep those open.
    while proc.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    _signal_group(proc, signal.SIGKILL)  # whatever is left, including stray children
    await proc.wait()


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, sig)
        elif proc.returncode is None:  # Windows: no process groups, just the CLI itself
            proc.kill()


def _child_env() -> dict[str, str]:
    """The user's environment minus Milo's own keys, which the agent never needs."""
    return {k: v for k, v in os.environ.items() if not k.startswith("MILO_")}


async def check_cli(executable: str = "claude") -> CliStatus:
    """Is `claude` on PATH and logged in? Uses `claude auth status`, so no model call."""
    path = shutil.which(executable)
    if path is None:
        return CliStatus(installed=False, problem=f"Claude Code isn't installed. {INSTALL_HINT}")

    version_out = await _capture(path, "--version")
    version = version_out.split()[0] if version_out else None
    try:
        auth = json.loads(await _capture(path, "auth", "status") or "")
    except ValueError:
        return CliStatus(
            installed=True,
            version=version,
            problem="Couldn't read `claude auth status`. Update with `claude update`.",
        )
    if not auth.get("loggedIn"):
        return CliStatus(
            installed=True,
            logged_in=False,
            version=version,
            problem=f"Claude Code isn't logged in. {LOGIN_HINT}",
        )
    return CliStatus(
        installed=True, logged_in=True, version=version, auth_method=auth.get("authMethod")
    )


async def _capture(*args: str, timeout: float = 15.0) -> str | None:
    """Run a quick command and return its stdout, or None if it fails or hangs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace").strip()
