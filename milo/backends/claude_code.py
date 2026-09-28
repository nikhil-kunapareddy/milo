"""Claude Code backend: the user's own `claude` CLI, run headless."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass

INSTALL_HINT = (
    "Install it with `curl -fsSL https://claude.ai/install.sh | bash` "
    "(more options: https://code.claude.com/docs/en/setup)."
)
LOGIN_HINT = "Log in once with `claude auth login`, then try again."


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
