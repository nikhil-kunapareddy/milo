"""Research: prompt, brief parsing, JSON retry, citation guard, and grounding."""

import json

import pytest

from milo.backends.base import Error, Final, SessionStarted, ToolCall, ToolResult
from milo.guard import UNVERIFIED
from milo.models import Audience, CollectorResult, Intake, SessionState
from milo.session import (
    BRIEF_SCHEMA,
    FOLLOW_UP_PROMPT,
    Ask,
    Controller,
    Say,
    ShowBrief,
    ShowText,
    Step,
    Working,
    research_prompt,
)
from milo.store import Store
from tests.conftest import FIXTURES, fixture_json
from tests.fakes import FakeBackend, FakeCollector, brief_data, reply, scripted

S = SessionState
BOSTON_MAG = "https://www.bostonmagazine.com/restaurants/best-ramen-in-boston/"
MADE_UP = "https://www.bostonglobe.com/2026/09/01/food/fenway-ramen-war-invented/"
OBSERVED = json.loads((FIXTURES / "briefs" / "observed_urls.json").read_text())


def places_collector() -> FakeCollector:
    """Real-shaped Places data: the text-search fixture, as the collector would store it."""
    raw = fixture_json("places/text_search_ok.json")["places"]
    places = [
        {
            "name": p["displayName"]["text"],
            "rating": p.get("rating"),
            "user_rating_count": p.get("userRatingCount"),
            "price_level": "$$",
            "maps_url": p["googleMapsUri"],
            "website": p.get("websiteUri"),
            "reviews": [{"rating": 3, "when": "a month ago", "text": "$19 is steep"}],
        }
        for p in raw
    ]
    urls = [p["maps_url"] for p in places]
    result = CollectorResult(
        source="google_places", status="ok", data={"query": "q", "places": places}, urls=urls
    )
    return FakeCollector(result, "Google Places")


def youtube_skipped() -> FakeCollector:
    result = CollectorResult(source="youtube", status="skipped", note="no key")
    return FakeCollector(result, "YouTube", available=False)


def searching(urls: list[str], brief: dict) -> list:
    """A research run: one search that surfaces `urls`, one read, then the brief."""
    return [
        SessionStarted("sess-123"),
        ToolCall(
            "t1",
            "WebSearch",
            {"query": "ramen boston"},
            "Searching the web: ramen boston",
            "search",
        ),
        ToolResult("t1", "WebSearch", "results", urls, True, "search"),
        ToolCall("t2", "WebFetch", {"url": BOSTON_MAG}, "Reading bostonmagazine.com", "read"),
        ToolResult("t2", "WebFetch", "page", [BOSTON_MAG], True, "read"),
        Final(json.dumps(brief), "sess-123", structured=brief),
    ]


async def researched(store, **replies) -> tuple[Controller, list]:
    backend = FakeBackend(
        scripted({"ramen in Boston": reply("ramen", "Fenway, Boston")}, **replies)
    )
    ctl = Controller(
        backend=backend, collectors=[places_collector(), youtube_skipped()], store=store
    )
    [_ async for _ in ctl.handle("ramen in Boston")]
    events = [e async for e in ctl.handle("2")]
    return ctl, events


@pytest.fixture
def store(home):
    return Store()


async def test_full_brief_with_verified_sources(store):
    ctl, events = await researched(store, research=[searching(OBSERVED, brief_data())])

    assert Step("Verified 11 of 12 citations, 1 removed", "ok") in events
    assert events[-2:] == [ShowBrief(ctl.session.brief), Ask(FOLLOW_UP_PROMPT)]
    assert ctl.state is S.FOLLOW_UP
    saved = store.load(ctl.session.id)
    assert saved.state is S.FOLLOW_UP
    assert saved.backend_session_id == "sess-123"
    assert len(saved.sources) == 11
    assert MADE_UP not in saved.model_dump_json()
    assert BOSTON_MAG in saved.observed_urls


async def test_progress_shows_each_tool_call(store):
    _, events = await researched(store, research=[searching(OBSERVED, brief_data())])
    assert Working("Searching the web: ramen boston") in events
    assert Working("Reading bostonmagazine.com") in events
    assert any(
        isinstance(e, Step) and e.text.startswith("Searched 1 times and read 1 pages")
        for e in events
    )


async def test_competitor_numbers_come_from_places(store):
    ctl, _ = await researched(store, research=[searching(OBSERVED, brief_data())])
    tora, santouka, ghost = ctl.session.brief.competitors
    assert (tora.rating, tora.review_count) == (4.5, 1287)  # the model said 4.9 and 99999
    assert (santouka.rating, santouka.review_count) == (4.4, 3321)
    assert (ghost.rating, ghost.review_count) == (None, None)  # not in Places: no numbers


async def test_research_call_uses_web_tools_the_schema_and_the_work_dir(store):
    ctl, _ = await researched(store, research=[searching(OBSERVED, brief_data())])
    call = ctl.backend.calls[-1]
    assert call["options"].web is True
    assert call["options"].schema == BRIEF_SCHEMA
    assert call["resume"] is None
    assert call["cwd"] == store.work_dir(ctl.session.id)


async def test_only_collector_urls_verified_when_the_agent_found_nothing(store):
    brief = brief_data()
    no_search = [SessionStarted("s"), Final("", "s", structured=brief)]
    _, events = await researched(store, research=[no_search])
    # Only the two Google Maps sources came from this session (via the Places collector).
    assert Step("Verified 2 of 12 citations, 10 removed", "ok") in events


async def test_invalid_json_is_retried_once_in_the_same_session(store):
    bad = brief_data(campaign_ideas=[])  # schema says exactly 5
    ctl, events = await researched(
        store, research=[searching(OBSERVED, bad)], repair=[brief_data()]
    )
    repair_call = ctl.backend.calls[-1]
    assert repair_call["resume"] == "sess-123"
    assert repair_call["options"].web is False
    assert "campaign_ideas" in repair_call["prompt"]  # the problem is named
    assert ctl.state is S.FOLLOW_UP
    assert ctl.session.brief is not None
    assert Step("Verified 11 of 12 citations, 1 removed", "ok") in events


async def test_invalid_twice_shows_raw_text_with_unverified_links_removed(store):
    raw = f"Here is your brief! Read {BOSTON_MAG} and {MADE_UP} for more."
    research = [*searching(OBSERVED, {})[:-1], Final(raw, "sess-123")]
    ctl, events = await researched(store, research=[research], repair=["still not json"])

    warning = next(e for e in events if isinstance(e, Say) and e.tone == "warn")
    assert "wrong format" in warning.text
    shown = next(e for e in events if isinstance(e, ShowText))
    assert BOSTON_MAG in shown.text
    assert MADE_UP not in shown.text and UNVERIFIED in shown.text
    assert Step("Removed 1 unverified links", "ok") in events
    assert ctl.state is S.FOLLOW_UP
    assert store.load(ctl.session.id).brief_raw == shown.text


async def test_running_out_of_turns_asks_for_a_wrap_up(store):
    research = [*searching(OBSERVED, {})[:-1], Error("max_turns", "turn limit", "sess-123")]
    ctl, events = await researched(store, research=[research], wrap_up=[brief_data()])
    assert ctl.backend.calls[-1]["resume"] == "sess-123"
    assert ctl.state is S.FOLLOW_UP
    assert Step("Verified 11 of 12 citations, 1 removed", "ok") in events


async def test_backend_error_keeps_research_state_and_collected_data(store):
    error = Error("not_logged_in", "Claude Code isn't logged in. Run `claude auth login`.")
    ctl, events = await researched(store, research=[[error]])
    assert Say(error.message, "error") in events
    assert ctl.state is S.RESEARCHING
    saved = store.load(ctl.session.id)
    assert saved.state is S.RESEARCHING
    assert saved.collectors[0].status == "ok"


async def test_brief_without_sources(store):
    brief = brief_data(sources=[])
    _, events = await researched(store, research=[searching(OBSERVED, brief)])
    assert Step("The brief came back without any citations", "skip") in events


# The prompt ------------------------------------------------------------------------------


def session_for_prompt(store, collectors: list[CollectorResult]):
    from milo.models import Session
    from milo.store import now

    intake = Intake(
        what="ramen",
        where="Fenway, Boston",
        audience=Audience.EXISTING,
        request="I run a ramen place near Fenway, want more students",
    )
    return Session(
        id="ramen-fenway-boston-0000",
        created=now(),
        updated=now(),
        state=S.RESEARCHING,
        intake=intake,
        backend="fake",
        collectors=collectors,
    )


def test_prompt_carries_intake_data_and_unavailable_sources(store):
    places = places_collector()._result
    skipped = CollectorResult(source="youtube", status="skipped", note="no key")
    prompt = research_prompt(session_for_prompt(store, [places, skipped]))

    assert "- What: ramen" in prompt and "- Where: Fenway, Boston" in prompt
    assert "- Who is asking: Existing restaurant" in prompt
    assert '"I run a ramen place near Fenway, want more students"' in prompt
    assert "### Google Places" in prompt and '"name": "Tora Ramen"' in prompt
    assert "$19 is steep" in prompt  # raw review text for the model to theme
    assert "YouTube is unavailable (no key). There are no video view counts" in prompt
    assert "Never invent data for an unavailable source" in prompt
    assert "At least one must be built\n  on pricing or an offer" in prompt


def test_prompt_without_places_forbids_ratings(store):
    failed = CollectorResult(source="google_places", status="error", note="quota exceeded")
    prompt = research_prompt(session_for_prompt(store, [failed]))
    assert "Google Places is unavailable (quota exceeded)." in prompt
    assert "set rating, review_count, and price_level to null for every competitor" in prompt
    assert "## Local data Milo already collected\n\nNone." in prompt
    assert "or from reviews in pages you read" in prompt


def test_prompt_never_contains_keys(store, monkeypatch):
    monkeypatch.setenv("MILO_PLACES_KEY", "AIzaSECRETKEY123456789")
    prompt = research_prompt(session_for_prompt(store, [places_collector()._result]))
    assert "AIzaSECRET" not in prompt
