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


def intake_replies(table: dict[str, dict | str | Error]) -> Responder:
    """Answer intake prompts from a table: message -> JSON reply, raw text, or an Error."""

    def respond(prompt: str, options: RunOptions, resume: str | None) -> list[BackendEvent]:
        reply = table[intake_message(prompt)]
        if isinstance(reply, Error):
            return [reply]
        if isinstance(reply, dict):
            return [SessionStarted("intake"), Final(json.dumps(reply), "intake", structured=reply)]
        return [SessionStarted("intake"), Final(reply, "intake")]

    return respond


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
