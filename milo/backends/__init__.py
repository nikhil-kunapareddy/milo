"""Agent backends. Claude Code is the default; Codex is a stub for now."""

from __future__ import annotations

from milo.backends.base import Backend
from milo.backends.claude_code import ClaudeCodeBackend
from milo.backends.codex import CodexBackend

DEFAULT_BACKEND = ClaudeCodeBackend.name
BACKENDS: dict[str, type[ClaudeCodeBackend] | type[CodexBackend]] = {
    ClaudeCodeBackend.name: ClaudeCodeBackend,
    CodexBackend.name: CodexBackend,
}


def get_backend(name: str | None = None) -> Backend:
    """The named backend, or Claude Code. Raises KeyError for an unknown name."""
    return BACKENDS[name or DEFAULT_BACKEND]()
