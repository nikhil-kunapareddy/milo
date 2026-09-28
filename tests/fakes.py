"""Test doubles: a scripted backend and a canned collector."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from milo.backends.base import (
    BackendEvent,
    BackendStatus,
    Error,
    Final,
    RunOptions,
    SessionStarted,
)
from milo.models import CollectorResult, Intake

FIXTURES = Path(__file__).parent / "fixtures"

Responder = Callable[[str, RunOptions, str | None], list[BackendEvent]]
READY = BackendStatus(installed=True, logged_in=True, version="test", auth_method="test")


class FakeBackend:
    name = "fake"
    label = "Fake Backend"

    def __init__(self, respond: Responder | None = None, status: BackendStatus = READY) -> None:
        self.respond = respond or (lambda prompt, options, resume: [])
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        return self.status.installed

    async def check(self) -> BackendStatus:
        return self.status

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        resume: str | None = None,
        options: RunOptions | None = None,
    ) -> AsyncIterator[BackendEvent]:
        options = options or RunOptions()
        self.calls.append({"prompt": prompt, "cwd": cwd, "resume": resume, "options": options})
        for event in self.respond(prompt, options, resume):
            yield event


def intake_message(prompt: str) -> str:
    """The user's text, as embedded in prompts/intake.md."""
    return prompt.split("<<<\n", 1)[1].split("\n>>>", 1)[0]


def prompt_kind(prompt: str) -> str:
    """Which of Milo's prompts this is, by a phrase each template contains."""
    for kind, phrase in (
        ("intake", "food business owner's message"),
        ("research", "Research the local"),
        ("repair", "wasn't a valid brief"),
        ("wrap_up", "out of research time"),
    ):
        if phrase in prompt:
            return kind
    return "follow_up"


def as_events(reply: dict | str | Error | list, session_id: str) -> list[BackendEvent]:
    if isinstance(reply, list):
        return reply
    if isinstance(reply, Error):
        return [reply]
    if isinstance(reply, dict):
        return [SessionStarted(session_id), Final(json.dumps(reply), session_id, structured=reply)]
    return [SessionStarted(session_id), Final(reply, session_id)]


def scripted(intake: dict[str, dict | str | Error] | None = None, **replies: Any) -> Responder:
    """Intake answers come from a table keyed by the user's message; every other prompt kind
    (research, repair, wrap_up, follow_up) pops the next reply from its own list."""
    queues = {kind: list(value) for kind, value in replies.items()}

    def respond(prompt: str, options: RunOptions, resume: str | None) -> list[BackendEvent]:
        kind = prompt_kind(prompt)
        if kind == "intake":
            return as_events((intake or {})[intake_message(prompt)], "intake")
        queue = queues.get(kind)
        if not queue:
            raise AssertionError(f"unexpected {kind} prompt")
        return as_events(queue.pop(0), resume or "research-session")

    return respond


def intake_replies(table: dict[str, dict | str | Error]) -> Responder:
    """Intake answers from a table; research gets a valid brief."""
    return scripted(table, research=[brief_data()] * 5)


def brief_data(**overrides: Any) -> dict:
    data = json.loads((FIXTURES / "briefs" / "brief_12_sources.json").read_text())
    return {**data, **overrides}


class FakeCollector:
    def __init__(self, result: CollectorResult, label: str = "Fake", available: bool = True):
        self.name = result.source
        self.label = label
        self._result = result
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    async def collect(self, intake: Intake) -> CollectorResult:
        self.calls += 1
        return self._result


def reply(what: str | None, where: str | None, on_topic: bool = True) -> dict:
    return {"what": what, "where": where, "on_topic": on_topic}


def places_result(n: int = 3, reviewed: int = 2) -> CollectorResult:
    places = [
        {
            "name": f"Place {i}",
            "maps_url": f"https://maps.google.com/?cid={i}",
            "website": None,
            **({"reviews": [{"rating": 5, "text": "great"}]} if i < reviewed else {}),
        }
        for i in range(n)
    ]
    return CollectorResult(
        source="google_places",
        status="ok",
        data={"query": "q", "places": places},
        urls=[p["maps_url"] for p in places],
    )
