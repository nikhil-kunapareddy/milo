"""Intake parser: the user's first message -> what and where."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from milo import prompts
from milo.backends.base import Backend, Error, Final, RunOptions
from milo.models import parse_json_object

MAX_WHAT = 60
MAX_WHERE = 80


class IntakeParse(BaseModel):
    what: str | None
    where: str | None
    on_topic: bool

    @field_validator("what", "where", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().strip(".\"'") or None
        return value

    @field_validator("what")
    @classmethod
    def _short_what(cls, value: str | None) -> str | None:
        return value.lower()[:MAX_WHAT] if value else value

    @field_validator("where")
    @classmethod
    def _short_where(cls, value: str | None) -> str | None:
        return value[:MAX_WHERE] if value else value


INTAKE_SCHEMA = IntakeParse.model_json_schema()
INTAKE_OPTIONS = RunOptions(web=False, schema=INTAKE_SCHEMA, ephemeral=True, max_turns=3)


@dataclass(frozen=True)
class IntakeFailure:
    """The backend can't run at all (not installed, not logged in): tell the user."""

    message: str


async def parse_intake(
    backend: Backend, message: str, *, cwd: Path
) -> IntakeParse | IntakeFailure | None:
    """Ask the backend to read the message. None means "couldn't parse": ask directly."""
    prompt = prompts.render("intake.md", message=message)
    final: Final | None = None
    async with aclosing(backend.run(prompt, cwd=cwd, options=INTAKE_OPTIONS)) as events:
        async for event in events:
            if isinstance(event, Error):
                if event.kind in ("not_installed", "not_logged_in"):
                    return IntakeFailure(event.message)
                return None
            if isinstance(event, Final):
                final = event
    if final is None:
        return None
    return parse_reply(final.structured if final.structured is not None else final.text)


def parse_reply(reply: dict[str, Any] | str) -> IntakeParse | None:
    data = parse_json_object(reply) if isinstance(reply, str) else reply
    if data is None:
        return None
    try:
        return IntakeParse.model_validate(data)
    except ValidationError:
        return None
