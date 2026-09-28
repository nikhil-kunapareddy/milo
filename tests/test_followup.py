"""Follow-ups (resumed backend session + citation guard), /sources, /report, and resume."""

import json
from datetime import date

import pytest

from milo.backends.base import Error, Final, SessionStarted, ToolCall, ToolResult
from milo.backends.stream import StreamParser
from milo.guard import UNVERIFIED
from milo.models import Brief, CollectorResult, Intake, Session, SessionState
from milo.session import (
    FOLLOW_UP_OPTIONS,
    FOLLOW_UP_PROMPT,
    Ask,
    Controller,
    Say,
    ShowBrief,
    ShowSources,
    ShowText,
    Step,
)
from milo.store import Store, now
from tests.conftest import FIXTURES
from tests.fakes import FakeBackend, brief_data, scripted

S = SessionState
BOSTON_MAG = "https://www.bostonmagazine.com/restaurants/best-ramen-in-boston/"
PATCH = "https://patch.com/massachusetts/boston/ramen-spot-opens-downtown-boston-food-hall"
MADE_UP = "https://www.bostonglobe.com/2026/09/01/food/fenway-ramen-war-invented/"


@pytest.fixture
def store(home):
    return Store()


def follow_up_session(store: Store, observed: list[str] | None = None) -> Session:
    first_source = {"id": 1, "title": "Boston Magazine", "url": BOSTON_MAG}
    brief = Brief.model_validate(brief_data(sources=[first_source]))
    session = Session(
        id="ramen-boston-7f3a",
        created=now(),
        updated=now(),
        state=S.FOLLOW_UP,
        intake=Intake(what="ramen", where="Boston"),
        backend="claude-code",
        backend_session_id="sess-123",
        collectors=[CollectorResult(source="youtube", status="skipped", note="no key")],
        brief=brief,
        sources=list(brief.sources),
        observed_urls=observed if observed is not None else [BOSTON_MAG, PATCH],
    )
    store.save(session)
    return session


def controller(store, tmp_path, **replies) -> Controller:
    session = follow_up_session(store)
    backend = FakeBackend(scripted({}, **replies))
    return Controller(
        backend=backend, collectors=[], store=store, session=session, report_dir=tmp_path
    )


async def send(ctl, text) -> list:
    return [event async for event in ctl.handle(text)]


def answer(text: str, *events) -> list:
    return [SessionStarted("sess-123"), *events, Final(text, "sess-123")]


async def test_follow_up_resumes_the_research_session(store, tmp_path):
    ctl = controller(store, tmp_path, follow_up=[answer("Tora has the weakest Instagram.")])
    events = await send(ctl, "which competitor has the weakest social presence?")

    call = ctl.backend.calls[0]
    assert call["resume"] == "sess-123"
    assert call["options"] == FOLLOW_UP_OPTIONS and call["options"].web is True
    assert call["cwd"] == store.work_dir("ramen-boston-7f3a")
    assert "<<<\nwhich competitor has the weakest social presence?\n>>>" in call["prompt"]
    assert events[-1] == ShowText("Tora has the weakest Instagram.")
    assert ctl.state is S.FOLLOW_UP


async def test_answer_links_are_guarded_and_the_turn_saved(store, tmp_path):
    text = f"See [Patch]({PATCH}) and [the Globe]({MADE_UP})."
    ctl = controller(store, tmp_path, follow_up=[answer(text)])
    events = await send(ctl, "what's new downtown?")

    assert Step("Verified 1 of 2 links, 1 removed", "ok") in events
    shown = events[-1].text
    assert PATCH in shown and MADE_UP not in shown and UNVERIFIED in shown
    saved = store.load(ctl.session.id)
    assert [t.question for t in saved.turns] == ["what's new downtown?"]
    assert saved.turns[0].answer == shown
    assert [(s.id, s.title, s.url) for s in saved.sources] == [
        (1, "Boston Magazine", BOSTON_MAG),
        (2, "Patch", PATCH),
    ]


async def test_new_research_in_a_follow_up_can_be_cited(store, tmp_path):
    fresh = "https://www.eater.com/boston/ramen-openings-2026"
    research = answer(
        f"Eater lists two openings: {fresh}",
        ToolCall("t1", "WebSearch", {"query": "q"}, "Searching the web: q", "search"),
        ToolResult("t1", "WebSearch", "results", [fresh], True, "search"),
    )
    ctl = controller(store, tmp_path, follow_up=[research])
    events = await send(ctl, "any new openings?")
    assert Step("Verified 1 of 1 links", "ok") in events
    saved = store.load(ctl.session.id)
    assert fresh in saved.observed_urls
    assert saved.sources[-1].url == fresh
    assert saved.sources[-1].title == "eater.com"  # bare link: titled by its site


async def test_citing_a_known_source_again_adds_nothing(store, tmp_path):
    ctl = controller(store, tmp_path, follow_up=[answer(f"As before: {BOSTON_MAG}")])
    await send(ctl, "remind me?")
    assert len(store.load(ctl.session.id).sources) == 1


async def test_off_topic_follow_up_is_redirected_and_not_saved(store, tmp_path):
    ctl = controller(store, tmp_path, follow_up=[answer("OFF_TOPIC")])
    events = await send(ctl, "can you fix my bash script?")
    [redirect] = [e for e in events if isinstance(e, Say)]
    assert redirect.tone == "warn"
    assert "Ask about marketing ramen in Boston" in redirect.text
    assert store.load(ctl.session.id).turns == []
    assert ctl.state is S.FOLLOW_UP


async def test_follow_up_error_keeps_the_session(store, tmp_path):
    error = Error("api_error", "Claude Code reported an error: overloaded")
    ctl = controller(store, tmp_path, follow_up=[[error]])
    events = await send(ctl, "what about delivery?")
    assert Say(error.message, "error") in events
    assert ctl.state is S.FOLLOW_UP
    assert store.load(ctl.session.id).turns == []


async def test_real_follow_up_transcript(store, tmp_path):
    """The captured Claude Code follow-up, parsed and guarded end to end."""
    parser = StreamParser()
    events = []
    for line in (FIXTURES / "stream" / "followup_resume.jsonl").read_text().splitlines():
        events += parser.feed(line)
    ctl = controller(store, tmp_path, follow_up=[events])
    shown = await send(ctl, "which competitor has the weakest social presence?")
    assert Step("Verified 1 of 1 links", "ok") in shown
    assert PATCH in shown[-1].text


# Commands --------------------------------------------------------------------------------


async def test_sources_lists_verified_sources(store, tmp_path):
    ctl = controller(store, tmp_path)
    [event] = await send(ctl, "/sources")
    assert event == ShowSources(ctl.session.sources)


async def test_report_writes_markdown_and_never_overwrites(store, tmp_path):
    ctl = controller(store, tmp_path)
    [first] = await send(ctl, "/report")
    [second] = await send(ctl, "/report")
    base = f"milo-ramen-boston-{date.today().isoformat()}"
    assert {p.name for p in tmp_path.glob("milo-*.md")} == {f"{base}.md", f"{base}-2.md"}
    assert first.tone == "ok" and first.text.endswith(f"{base}.md")
    assert second.text.endswith(f"{base}-2.md")
    assert "## Market snapshot" in (tmp_path / f"{base}.md").read_text()


async def test_resumed_follow_up_session_shows_the_brief(store, tmp_path):
    session = follow_up_session(store)
    session.turns = []
    ctl = Controller(backend=FakeBackend(), collectors=[], store=store, session=session)
    opening = ctl.opening()
    assert opening[0] == Say("Resumed ramen-boston-7f3a: ramen in Boston", "muted")
    assert opening[1] == ShowBrief(session.brief)
    assert opening[-1] == Ask(FOLLOW_UP_PROMPT)


async def test_resumed_raw_brief_session_shows_the_text(store):
    session = follow_up_session(store)
    session.brief, session.brief_raw = None, "Raw findings"
    ctl = Controller(backend=FakeBackend(), collectors=[], store=store, session=session)
    assert ShowText("Raw findings", "Brief (raw)") in ctl.opening()


def test_saved_session_has_no_keys(store, monkeypatch):
    monkeypatch.setenv("MILO_PLACES_KEY", "AIzaSECRETKEY123456789")
    follow_up_session(store)
    raw = (store.root / "ramen-boston-7f3a" / "session.json").read_text()
    assert "SECRET" not in raw
    assert json.loads(raw)["state"] == "follow_up"
