"""Codex backend: a placeholder so the interface has a second implementation."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from milo.backends.base import BackendEvent, BackendStatus, RunOptions

NOT_READY = "Codex backend coming soon — use Claude Code for now"


class CodexBackend:
    name = "codex"
    label = "Codex"

    def available(self) -> bool:
        return shutil.which("codex") is not None

    async def check(self) -> BackendStatus:
        return BackendStatus(installed=self.available(), problem=NOT_READY)

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        resume: str | None = None,
        options: RunOptions | None = None,
    ) -> AsyncIterator[BackendEvent]:
        raise NotImplementedError(NOT_READY)
