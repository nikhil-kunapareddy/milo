"""Parse Claude Code's `--output-format stream-json` output into BackendEvents.

Shapes confirmed against Claude Code 2.1.283 (real transcripts in tests/fixtures/stream/):

- `system/init` carries `session_id`.
- `assistant` events carry one content block each: `thinking`, `text`, or `tool_use`.
- `user` events carry a `tool_result` block plus a top-level `tool_use_result` with
  structured data: WebSearch gives `results[].content[] = {title, url}`, WebFetch gives
  `{url, code, result}`. A failed fetch is a normal result with `code: 404`.
- `result` carries the final text, `is_error`, `subtype`, and `structured_output`. A
  logged-out CLI reports `subtype: "success"` with `is_error: true`, so check `is_error`.

Anything else (rate_limit_event, thinking_tokens, hook events) is ignored.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from milo.backends.base import (
    BackendEvent,
    Error,
    Final,
    Progress,
    SessionStarted,
    ToolCall,
    ToolResult,
)

WEB_SEARCH = "WebSearch"
WEB_FETCH = "WebFetch"
WEB_TOOLS = (WEB_SEARCH, WEB_FETCH)
STRUCTURED_OUTPUT = "StructuredOutput"  # the tool --json-schema adds; internal plumbing

LOGIN_HINT = "Log in once with `claude auth login`, then try again."
_LINKS_LINE = re.compile(r"^Links: (\[.*\])$", re.MULTILINE)


class StreamParser:
    """Feed it stdout lines in order; it returns the events each line produces."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.finished = False  # saw the closing `result` event
        self._tool_names: dict[str, str] = {}

    def feed(self, line: str) -> list[BackendEvent]:
        try:
            event = json.loads(line)
        except ValueError:
            return []  # blank or partial line: nothing to report
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            return self._init(event)
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._user(event)
        if kind == "result":
            return self._result(event)
        return []

    def finish(self, returncode: int | None, stderr: str) -> list[BackendEvent]:
        """Call once the process exits; reports a crash if no result ever arrived."""
        if self.finished:
            return []
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {returncode}"
        return [
            Error("crashed", f"Claude Code stopped before finishing ({detail}).", self.session_id)
        ]

    def _init(self, event: dict[str, Any]) -> list[BackendEvent]:
        session_id = event.get("session_id")
        if not isinstance(session_id, str):
            return []
        self.session_id = session_id
        return [SessionStarted(session_id)]

    def _assistant(self, event: dict[str, Any]) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        for block in _blocks(event):
            if block.get("type") == "text" and (text := str(block.get("text", "")).strip()):
                events.append(Progress(text))
            elif block.get("type") == "tool_use":
                call_id, name = str(block.get("id", "")), str(block.get("name", ""))
                self._tool_names[call_id] = name
                if name == STRUCTURED_OUTPUT:
                    continue
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                events.append(ToolCall(call_id, name, tool_input, _summary(name, tool_input)))
        return events

    def _user(self, event: dict[str, Any]) -> list[BackendEvent]:
        results = [b for b in _blocks(event) if b.get("type") == "tool_result"]
        # `tool_use_result` describes the message's result; only trust it when there's one.
        structured = event.get("tool_use_result") if len(results) == 1 else None
        events: list[BackendEvent] = []
        for block in results:
            call_id = str(block.get("tool_use_id", ""))
            name = self._tool_names.get(call_id, "")
            if name == STRUCTURED_OUTPUT:
                continue
            content = _text(block.get("content"))
            urls, ok = _observed_urls(name, structured, content)
            ok = ok and not block.get("is_error", False)
            events.append(ToolResult(call_id, name, content, urls, ok))
        return events

    def _result(self, event: dict[str, Any]) -> list[BackendEvent]:
        self.finished = True
        session_id = event.get("session_id") or self.session_id
        text = event.get("result") if isinstance(event.get("result"), str) else ""
        if event.get("is_error") or event.get("subtype") != "success":
            return [_error(event, text, session_id)]
        structured = event.get("structured_output")
        cost = event.get("total_cost_usd")
        return [
            Final(
                text=text,
                session_id=session_id,
                structured=structured if isinstance(structured, dict) else None,
                cost_usd=cost if isinstance(cost, int | float) else None,
            )
        ]


def _error(event: dict[str, Any], text: str, session_id: str | None) -> Error:
    lowered = text.lower()
    if "not logged in" in lowered or "/login" in lowered:
        return Error("not_logged_in", f"Claude Code isn't logged in. {LOGIN_HINT}", session_id)
    if event.get("subtype") == "error_max_turns" or event.get("terminal_reason") == "max_turns":
        return Error("max_turns", "The research hit its turn limit before finishing.", session_id)
    errors = event.get("errors") if isinstance(event.get("errors"), list) else []
    detail = text or "; ".join(str(e) for e in errors) or "unknown error"
    return Error("api_error", f"Claude Code reported an error: {detail}", session_id)


def _observed_urls(name: str, structured: Any, content: str) -> tuple[list[str], bool]:
    """URLs the agent really saw, from structured fields only (never from summary prose)."""
    data = structured if isinstance(structured, dict) else {}
    if name == WEB_SEARCH:
        urls = [
            item["url"]
            for block in data.get("results", [])
            if isinstance(block, dict)
            for item in block.get("content", [])
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
        return (urls or _links_from_text(content)), True
    if name == WEB_FETCH:
        code, url = data.get("code"), data.get("url")
        ok = isinstance(code, int) and 200 <= code < 300
        return ([url] if ok and isinstance(url, str) else []), ok
    return [], True


def _links_from_text(content: str) -> list[str]:
    """Fallback for WebSearch: the result text embeds the same list as `Links: [...]` JSON."""
    urls: list[str] = []
    for match in _LINKS_LINE.finditer(content):
        try:
            links = json.loads(match.group(1))
        except ValueError:
            continue
        urls += [x["url"] for x in links if isinstance(x, dict) and isinstance(x.get("url"), str)]
    return urls


def _summary(name: str, tool_input: dict[str, Any]) -> str:
    if name == WEB_SEARCH:
        return f"Searching the web: {tool_input.get('query', '')}".strip()
    if name == WEB_FETCH:
        host = urlparse(str(tool_input.get("url", ""))).netloc.removeprefix("www.")
        return f"Reading {host or 'a page'}"
    return f"Using {name}"


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""
