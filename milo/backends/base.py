"""The Backend protocol and the events every backend streams back."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SessionStarted:
    session_id: str  # arrives first, so a crash mid-run can still be resumed


@dataclass(frozen=True)
class Progress:
    text: str  # the agent narrating what it's doing


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    summary: str  # one line for the user: "Searching the web: ramen boston"


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    urls: list[str] = field(default_factory=list)  # URLs this result actually showed the agent
    ok: bool = True


@dataclass(frozen=True)
class Final:
    text: str
    session_id: str | None
    structured: dict[str, Any] | None = None  # set when a schema was requested and met
    cost_usd: float | None = None


ErrorKind = Literal[
    "not_installed", "not_logged_in", "max_turns", "api_error", "crashed", "unsupported"
]


@dataclass(frozen=True)
class Error:
    kind: ErrorKind
    message: str  # user-facing, with the fix when there is one
    session_id: str | None = None


BackendEvent = SessionStarted | Progress | ToolCall | ToolResult | Final | Error


@dataclass(frozen=True)
class RunOptions:
    web: bool = True  # allow web search and fetch; nothing else is ever allowed
    schema: dict[str, Any] | None = None  # JSON Schema the final answer must match
    ephemeral: bool = False  # don't keep a resumable session (intake parsing)
    max_turns: int = 30


class Backend(Protocol):
    name: str  # "claude-code"
    label: str  # "Claude Code"

    def available(self) -> bool: ...

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        resume: str | None = None,
        options: RunOptions | None = None,
    ) -> AsyncIterator[BackendEvent]: ...
