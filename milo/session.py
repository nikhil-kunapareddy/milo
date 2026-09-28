"""Session controller: an explicit state machine that decides which inputs are allowed.

INTAKE -> AUDIENCE -> COLLECTING -> RESEARCHING -> FOLLOW_UP (loops) -> ENDED

The controller never prints. It yields UI events and the CLI decides how they look.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError

from milo import collectors as sources
from milo import prompts
from milo.backends.base import (
    Backend,
    Error,
    Final,
    Progress,
    RunOptions,
    SessionStarted,
    ToolCall,
    ToolResult,
)
from milo.collectors.base import Collector, collect_all
from milo.guard import CitationGuard, ground_competitors
from milo.intake import IntakeFailure, parse_intake
from milo.models import (
    AUDIENCE_LABELS,
    Audience,
    Brief,
    Intake,
    Session,
    SessionState,
    parse_json_object,
)
from milo.store import Store, now

State = SessionState


class InputKind(StrEnum):
    TEXT = "text"
    EMPTY = "empty"
    COMMAND = "command"


# Which kinds of input each state accepts. Anything else gets HINTS[state] and no change.
ACCEPTS: dict[State, frozenset[InputKind]] = {
    State.INTAKE: frozenset({InputKind.TEXT, InputKind.COMMAND}),
    State.AUDIENCE: frozenset({InputKind.TEXT, InputKind.EMPTY, InputKind.COMMAND}),
    # Nothing is read while these run; Enter only matters after an interrupted run.
    State.COLLECTING: frozenset({InputKind.EMPTY, InputKind.COMMAND}),
    State.RESEARCHING: frozenset({InputKind.EMPTY, InputKind.COMMAND}),
    State.FOLLOW_UP: frozenset({InputKind.TEXT, InputKind.COMMAND}),
    State.ENDED: frozenset(),
}

TRANSITIONS: dict[State, frozenset[State]] = {
    State.INTAKE: frozenset({State.AUDIENCE, State.ENDED}),
    State.AUDIENCE: frozenset({State.COLLECTING, State.ENDED}),
    State.COLLECTING: frozenset({State.RESEARCHING, State.ENDED}),
    State.RESEARCHING: frozenset({State.FOLLOW_UP, State.ENDED}),
    State.FOLLOW_UP: frozenset({State.FOLLOW_UP, State.ENDED}),
    State.ENDED: frozenset(),
}

EXAMPLE = '"ramen in Boston"'
INTAKE_QUESTION = f"What do you want to market, and where?  (e.g. {EXAMPLE})"
WHAT_QUESTION = "What do you want to market?  (e.g. ramen, tacos, a vegan bakery)"
WHERE_QUESTION = "Where is it?  (e.g. Boston, or Fenway, Boston)"
OFF_TOPIC = (
    "Milo researches marketing for restaurants and food businesses. "
    f'Try {EXAMPLE} or "I run a taco truck in Austin".'
)
AUDIENCE_QUESTION = (
    "Who is this for?  "
    + "  ".join(f"[{i}] {label}" for i, label in enumerate(AUDIENCE_LABELS.values(), 1))
    + "  (Enter to skip)"
)
CONTINUE_QUESTION = "Press Enter to continue the research, or /exit."
FOLLOW_UP_PROMPT = "Ask a follow-up, or /report, /sources, /exit"

HINTS: dict[State, str] = {
    State.INTAKE: f"Tell me what you want to market and where, e.g. {EXAMPLE}.",
    State.AUDIENCE: "Pick 1–4, or press Enter to skip.",
    State.COLLECTING: CONTINUE_QUESTION,
    State.RESEARCHING: CONTINUE_QUESTION,
    State.FOLLOW_UP: f"{FOLLOW_UP_PROMPT}.",
    State.ENDED: "This session has ended.",
}

COMMANDS = {
    "/report": "write a Markdown report to this folder",
    "/sources": "list the verified sources so far",
    "/exit": "save the session and quit",
    "/help": "show this list",
}
NOTHING_YET = "Nothing yet: that works once the first brief is ready."

AUDIENCE_WORDS = {
    Audience.NEW_OPENING: ("new", "opening", "open"),
    Audience.EXISTING: ("existing", "restaurant", "current"),
    Audience.CREATOR: ("creator", "influencer", "content"),
    Audience.CHAIN: ("chain", "location", "franchise"),
}


# UI events -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Say:
    text: str
    tone: Literal["info", "ok", "warn", "error", "muted"] = "info"


@dataclass(frozen=True)
class Ask:
    text: str


@dataclass(frozen=True)
class Step:
    text: str
    status: Literal["ok", "skip", "work", "fail"]


@dataclass(frozen=True)
class Working:
    text: str  # transient: replaced by the next Working or cleared by any other event


@dataclass(frozen=True)
class ShowBrief:
    brief: Brief


@dataclass(frozen=True)
class ShowText:
    text: str  # markdown from the model, already through the citation guard
    title: str = ""


UIEvent = Say | Ask | Step | Working | ShowBrief | ShowText

BRIEF_SCHEMA = Brief.model_json_schema()
RESEARCH_OPTIONS = RunOptions(web=True, schema=BRIEF_SCHEMA, max_turns=30)
REPAIR_OPTIONS = RunOptions(web=False, schema=BRIEF_SCHEMA, max_turns=4)


@dataclass
class RunResult:
    """What one backend run produced, filled in as its events stream past."""

    final: Final | None = None
    error: Error | None = None
    searches: int = 0
    pages: int = 0
    seen: list[str] = field(default_factory=list)


class InvalidTransition(RuntimeError):
    pass


def classify(text: str) -> InputKind:
    if not text:
        return InputKind.EMPTY
    return InputKind.COMMAND if text.startswith("/") else InputKind.TEXT


def parse_audience(text: str) -> Audience | None:
    choices = list(Audience)
    cleaned = text.strip().strip("[]().").lower()
    if cleaned.isdigit() and 1 <= int(cleaned) <= len(choices):
        return choices[int(cleaned) - 1]
    matches = [a for a, words in AUDIENCE_WORDS.items() if any(w in cleaned for w in words)]
    return matches[0] if len(matches) == 1 else None


# Controller ----------------------------------------------------------------------------


class Controller:
    def __init__(
        self,
        *,
        backend: Backend,
        collectors: Sequence[Collector],
        store: Store,
        session: Session | None = None,
    ) -> None:
        self.backend = backend
        self.collectors = collectors
        self.store = store
        self.session = session
        self.state = session.state if session else State.INTAKE
        # Intake progress lives in memory: nothing is saved until what and where are known.
        self._request = ""
        self._asking: Literal["request", "what", "where"] = "request"
        self._direct = False  # parsing failed: take answers exactly as typed
        self._what: str | None = None
        self._where: str | None = None

    def opening(self) -> list[UIEvent]:
        """What to show before the first input: the first question, or where we left off."""
        if self.session is None:
            return [Ask(INTAKE_QUESTION)]
        intake = self.session.intake
        resumed = Say(f"Resumed {self.session.id}: {intake.what} in {intake.where}", "muted")
        next_question = {
            State.AUDIENCE: AUDIENCE_QUESTION,
            State.COLLECTING: CONTINUE_QUESTION,
            State.RESEARCHING: CONTINUE_QUESTION,
        }.get(self.state, FOLLOW_UP_PROMPT)
        return [resumed, Ask(next_question)]

    def hint(self) -> str:
        return HINTS[self.state]

    async def handle(self, raw: str) -> AsyncIterator[UIEvent]:
        text = raw.strip()
        kind = classify(text)
        if kind not in ACCEPTS[self.state]:
            yield Say(HINTS[self.state], "muted")
            return
        if kind is InputKind.COMMAND:
            handler = self._command(text)
        elif self.state is State.INTAKE:
            handler = self._intake(text)
        elif self.state is State.AUDIENCE:
            handler = self._audience(text)
        elif self.state in (State.COLLECTING, State.RESEARCHING):
            handler = self._run()
        else:
            handler = self._follow_up(text)
        async for event in handler:
            yield event

    # States ----------------------------------------------------------------------------

    async def _intake(self, text: str) -> AsyncIterator[UIEvent]:
        if self._direct:
            self._take_answer(text)
        else:
            if self._asking == "request":
                message = text
            else:  # answering a follow-up question: re-read it with the original request
                message = f"{self._request}\n{self._asking.capitalize()}: {text}"
            yield Working("Reading your request…")
            parsed = await parse_intake(self.backend, message, cwd=self.store.scratch_dir())
            if isinstance(parsed, IntakeFailure):
                yield Say(parsed.message, "error")
                return
            if parsed is None:  # unreadable reply: ask for each part directly from now on
                self._direct = True
                if self._asking == "request":
                    self._request = text
                    self._asking = "what"
                    yield Say("I couldn't read that automatically. Let's go one step at a time.")
                    yield Ask(WHAT_QUESTION)
                    return
                self._take_answer(text)
            elif not parsed.on_topic and self._asking == "request":
                yield Say(OFF_TOPIC, "warn")
                yield Ask(INTAKE_QUESTION)
                return
            else:
                if self._asking == "request":
                    self._request = text
                self._what = parsed.what or self._what
                self._where = parsed.where or self._where

        if self._what and self._where:
            self._start_session(Intake(what=self._what, where=self._where, request=self._request))
            yield Ask(AUDIENCE_QUESTION)
        elif self._what:
            self._asking = "where"
            yield Ask(WHERE_QUESTION)
        elif self._where:
            self._asking = "what"
            yield Ask(WHAT_QUESTION)
        else:
            self._asking = "request"
            yield Ask(INTAKE_QUESTION)

    async def _audience(self, text: str) -> AsyncIterator[UIEvent]:
        session = self._require_session()
        if text:
            audience = parse_audience(text)
            if audience is None:
                yield Say(HINTS[State.AUDIENCE], "muted")
                return
            session.intake.audience = audience
        self._transition(State.COLLECTING)
        async for event in self._run():
            yield event

    async def _run(self) -> AsyncIterator[UIEvent]:
        """Collect (unless already done), then research. Safe to re-run after an interruption."""
        session = self._require_session()
        if self.state is State.COLLECTING:
            yield Working("Collecting local data…")
            results = await collect_all(self.collectors, session.intake)
            session.collectors = results
            for url in (u for r in results for u in r.urls):
                if url not in session.observed_urls:
                    session.observed_urls.append(url)
            for result in results:
                if result.status == "ok":
                    for line in sources.summary(result):
                        yield Step(line, "ok")
                    if result.note:
                        yield Say(f"  {result.note}", "muted")
                else:
                    label = sources.LABELS.get(result.source, result.source)
                    yield Step(f"{label} skipped ({result.note})", "skip")
            self._transition(State.RESEARCHING)
        async for event in self._research():
            yield event

    async def _research(self) -> AsyncIterator[UIEvent]:
        session = self._require_session()
        guard = CitationGuard(session.observed_urls)
        started = time.monotonic()
        yield Step("Researching local marketing and best practices…", "work")

        run = RunResult()
        async for event in self._stream(research_prompt(session), guard, run, RESEARCH_OPTIONS):
            yield event
        if run.error and run.error.kind == "max_turns" and session.backend_session_id:
            # Out of turns mid-research: ask for the brief from what it found so far.
            yield Working("Wrapping up with what it found so far…")
            run = RunResult(searches=run.searches, pages=run.pages)
            wrap_up = prompts.render("wrap_up.md")
            async for event in self._stream(wrap_up, guard, run, REPAIR_OPTIONS, resume=True):
                yield event
        if run.error or run.final is None:
            message = run.error.message if run.error else "The research ended without a brief."
            yield Say(message, "error")
            yield Ask(CONTINUE_QUESTION)
            return  # still RESEARCHING: Enter tries again, collector data is kept
        took = _duration(time.monotonic() - started)
        yield Step(f"Searched {run.searches} times and read {run.pages} pages ({took})", "ok")

        brief, problem = parse_brief(run.final)
        if brief is None:  # one retry in the same session, asking for valid JSON only
            yield Working("Fixing the brief's format…")
            retry = RunResult()
            repair = prompts.render("repair.md", problem=problem)
            async for event in self._stream(repair, guard, retry, REPAIR_OPTIONS, resume=True):
                yield event
            brief, problem = parse_brief(retry.final)
            # Show the research reply itself: it holds the findings; the retry may be junk.
            raw = run.final.text or (retry.final.text if retry.final else "")
        if brief is None:
            text, removed = guard.clean_text(raw)
            session.brief_raw = text
            session.observed_urls = guard.observed
            self._transition(State.FOLLOW_UP)
            warning = f"The brief came back in the wrong format ({problem}). Here's the raw text."
            yield Say(warning, "warn")
            if removed:
                yield Step(f"Removed {removed} unverified links", "ok")
            yield ShowText(text, "Brief (raw)")
            yield Ask(FOLLOW_UP_PROMPT)
            return

        check = guard.check_brief(ground_competitors(brief, _places(session)))
        removed_note = f", {len(check.removed)} removed" if check.removed else ""
        if check.total:
            yield Step(f"Verified {check.kept} of {check.total} citations{removed_note}", "ok")
        else:
            yield Step("The brief came back without any citations", "skip")
        session.brief = check.brief
        session.sources = list(check.brief.sources)
        session.observed_urls = guard.observed
        self._transition(State.FOLLOW_UP)
        yield ShowBrief(check.brief)
        yield Ask(FOLLOW_UP_PROMPT)

    async def _stream(
        self,
        prompt: str,
        guard: CitationGuard,
        run: RunResult,
        options: RunOptions,
        *,
        resume: bool = False,
    ) -> AsyncIterator[UIEvent]:
        """Run the backend once, turning its events into progress and filling in `run`."""
        session = self._require_session()
        events = self.backend.run(
            prompt,
            cwd=self.store.work_dir(session.id),
            resume=session.backend_session_id if resume else None,
            options=options,
        )
        async with aclosing(events) as stream:
            async for event in stream:
                if isinstance(event, SessionStarted):
                    if session.backend_session_id != event.session_id:
                        session.backend_session_id = event.session_id
                        self.store.save(session)
                elif isinstance(event, ToolCall):
                    run.searches += event.kind == "search"
                    yield Working(event.summary)
                elif isinstance(event, ToolResult):
                    run.pages += event.kind == "read" and event.ok
                    if event.urls:
                        guard.observe(event.urls)
                        session.observed_urls = guard.observed
                        self.store.save(session)
                elif isinstance(event, Progress):
                    yield Working(_one_line(event.text))
                elif isinstance(event, Final):
                    run.final = event
                elif isinstance(event, Error):
                    run.error = event

    async def _follow_up(self, text: str) -> AsyncIterator[UIEvent]:
        yield Say("Follow-ups aren't connected yet.", "warn")

    # Commands --------------------------------------------------------------------------

    async def _command(self, text: str) -> AsyncIterator[UIEvent]:
        name = text.split()[0].lower()
        if name == "/help":
            yield Say("\n".join(f"{cmd:<9} {what}" for cmd, what in COMMANDS.items()))
        elif name == "/exit":
            if self.session is not None:
                self.store.save(self.session)
                sid = self.session.id
                yield Say(f"Saved session {sid}. Reopen it with `milo resume {sid}`.", "ok")
            self._transition(State.ENDED)
        elif name in ("/report", "/sources"):
            if self.session is None or (self.session.brief is None and not self.session.brief_raw):
                yield Say(NOTHING_YET, "muted")
            else:
                yield Say(f"{name} isn't connected yet.", "warn")
        else:
            yield Say(f"Unknown command {name}. Try /help.", "muted")

    # Helpers ---------------------------------------------------------------------------

    def _take_answer(self, text: str) -> None:
        """Use the answer exactly as typed for whichever part we just asked about."""
        if self._asking == "what":
            self._what = text
        elif self._asking == "where":
            self._where = text

    def _start_session(self, intake: Intake) -> None:
        created = now()
        self.session = Session(
            id=self.store.new_id(intake),
            created=created,
            updated=created,
            state=State.INTAKE,
            intake=intake,
            backend=self.backend.name,
        )
        self._transition(State.AUDIENCE)

    def _transition(self, new: State) -> None:
        if new not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state} -> {new}")
        self.state = new
        if self.session is not None and new is not State.ENDED:
            self.session.state = new  # ENDED is never stored, so resume lands somewhere useful
            self.store.save(self.session)

    def _require_session(self) -> Session:
        if self.session is None:
            raise InvalidTransition(f"{self.state} needs a session")
        return self.session


# Research helpers ----------------------------------------------------------------------


def research_prompt(session: Session) -> str:
    """The research instructions plus every collector result, skipped ones included."""
    available, unavailable = [], []
    for result in session.collectors:
        label = sources.LABELS.get(result.source, result.source)
        if result.status == "ok":
            data = json.dumps(result.data, indent=1, ensure_ascii=False)
            available.append({"label": label, "data": data, "note": result.note})
        else:
            note = result.note or "unavailable"
            unavailable.append({"label": label, "source": result.source, "note": note})
    audience = session.intake.audience
    return prompts.render(
        "research.md",
        intake=session.intake,
        audience=audience.label if audience else None,
        available=available,
        unavailable=unavailable,
        has_reviews=any(p.get("reviews") for p in _places(session) or []),
    )


def parse_brief(final: Final | None) -> tuple[Brief | None, str]:
    """The brief, or None and a short description of what was wrong with it."""
    if final is None:
        return None, "there was no reply"
    data = final.structured if final.structured is not None else parse_json_object(final.text)
    if data is None:
        return None, "the reply wasn't JSON"
    try:
        return Brief.model_validate(data), ""
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or 'brief'}: {e['msg']}" for e in exc.errors()
        ]
        return None, "; ".join(problems[:3])


def _places(session: Session) -> list[dict[str, Any]] | None:
    for result in session.collectors:
        if result.source == "google_places" and result.status == "ok" and result.data:
            return result.data.get("places")
    return None


def _one_line(text: str, limit: int = 80) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _duration(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"
