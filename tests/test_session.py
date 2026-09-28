"""State machine transitions, driven through the controller with fakes."""

import pytest

from milo.backends.base import Error
from milo.models import Audience, CollectorResult, SessionState
from milo.session import (
    ACCEPTS,
    AUDIENCE_QUESTION,
    HINTS,
    INTAKE_QUESTION,
    NOTHING_YET,
    OFF_TOPIC,
    TRANSITIONS,
    WHAT_QUESTION,
    WHERE_QUESTION,
    Ask,
    Controller,
    InvalidTransition,
    Say,
    Step,
    parse_audience,
)
from milo.store import Store
from tests.fakes import FakeBackend, FakeCollector, intake_replies, places_result, reply

S = SessionState
RAMEN = "I run a ramen place near Fenway, want more students"


@pytest.fixture
def store(home):
    return Store()


def controller(store, table=None, collectors=None) -> Controller:
    backend = FakeBackend(intake_replies(table or {}))
    skipped = CollectorResult(source="youtube", status="skipped", note="no key")
    collectors = collectors or [FakeCollector(skipped, "YouTube", available=False)]
    return Controller(backend=backend, collectors=collectors, store=store)


async def send(ctl: Controller, text: str) -> list:
    return [event async for event in ctl.handle(text)]


def texts(events: list) -> list[str]:
    return [e.text for e in events if hasattr(e, "text")]


# The tables themselves -------------------------------------------------------------------


def test_every_state_has_rules():
    assert set(ACCEPTS) == set(S) == set(TRANSITIONS) == set(HINTS)


def test_ended_is_terminal_and_accepts_nothing():
    assert TRANSITIONS[S.ENDED] == frozenset()
    assert ACCEPTS[S.ENDED] == frozenset()


def test_every_live_state_can_exit():
    for state in S:
        if state is not S.ENDED:
            assert S.ENDED in TRANSITIONS[state]


def test_illegal_transition_raises(store):
    ctl = controller(store)
    with pytest.raises(InvalidTransition):
        ctl._transition(S.FOLLOW_UP)


# INTAKE ----------------------------------------------------------------------------------


async def test_opening_asks_what_and_where(store):
    assert controller(store).opening() == [Ask(INTAKE_QUESTION)]


async def test_off_topic_is_redirected_and_state_kept(store):
    ctl = controller(store, {"fix my python loop": reply(None, None, on_topic=False)})
    events = await send(ctl, "fix my python loop")
    assert Say(OFF_TOPIC, "warn") in events
    assert events[-1] == Ask(INTAKE_QUESTION)
    assert ctl.state is S.INTAKE
    assert ctl.session is None
    assert not store.root.exists()  # nothing is saved before what and where are known


async def test_empty_input_in_intake_gets_a_hint(store):
    ctl = controller(store)
    assert await send(ctl, "   ") == [Say(HINTS[S.INTAKE], "muted")]
    assert ctl.state is S.INTAKE


async def test_valid_request_moves_to_audience_and_saves(store):
    ctl = controller(store, {RAMEN: reply("ramen", "Fenway, Boston")})
    events = await send(ctl, RAMEN)
    assert events[-1] == Ask(AUDIENCE_QUESTION)
    assert ctl.state is S.AUDIENCE
    saved = store.load(ctl.session.id)
    assert (saved.intake.what, saved.intake.where) == ("ramen", "Fenway, Boston")
    assert saved.intake.request == RAMEN
    assert saved.state is S.AUDIENCE
    assert ctl.session.id.startswith("ramen-fenway-boston-")


async def test_missing_where_asks_only_for_where(store):
    table = {"tacos": reply("tacos", None), "tacos\nWhere: Austin": reply("tacos", "Austin")}
    ctl = controller(store, table)
    assert (await send(ctl, "tacos"))[-1] == Ask(WHERE_QUESTION)
    assert ctl.state is S.INTAKE
    assert (await send(ctl, "Austin"))[-1] == Ask(AUDIENCE_QUESTION)
    assert (ctl.session.intake.what, ctl.session.intake.where) == ("tacos", "Austin")


async def test_missing_what_asks_only_for_what(store):
    table = {
        "marketing in Denver": reply(None, "Denver"),
        "marketing in Denver\nWhat: pho": reply("pho", "Denver"),
    }
    ctl = controller(store, table)
    assert (await send(ctl, "marketing in Denver"))[-1] == Ask(WHAT_QUESTION)
    assert (await send(ctl, "pho"))[-1] == Ask(AUDIENCE_QUESTION)
    assert ctl.session.intake.what == "pho"


async def test_unparseable_reply_falls_back_to_direct_questions(store):
    ctl = controller(store, {"bbq plz": "Sorry, I can't produce JSON today."})
    events = await send(ctl, "bbq plz")
    assert events[-1] == Ask(WHAT_QUESTION)
    assert (await send(ctl, "Texas BBQ"))[-1] == Ask(WHERE_QUESTION)
    assert (await send(ctl, "Lockhart, TX"))[-1] == Ask(AUDIENCE_QUESTION)
    intake = ctl.session.intake
    assert (intake.what, intake.where, intake.request) == ("Texas BBQ", "Lockhart, TX", "bbq plz")
    assert len(ctl.backend.calls) == 1  # direct answers never go back to the model


async def test_backend_not_logged_in_is_shown_and_state_kept(store):
    error = Error("not_logged_in", "Claude Code isn't logged in. Run `claude auth login`.")
    ctl = controller(store, {"ramen in Boston": error})
    events = await send(ctl, "ramen in Boston")
    assert Say(error.message, "error") in events
    assert ctl.state is S.INTAKE


async def test_other_backend_errors_fall_back_to_direct_questions(store):
    ctl = controller(store, {"ramen in Boston": Error("api_error", "overloaded")})
    assert (await send(ctl, "ramen in Boston"))[-1] == Ask(WHAT_QUESTION)


async def test_intake_call_has_no_tools_and_keeps_no_session(store):
    ctl = controller(store, {"ramen in Boston": reply("ramen", "Boston")})
    await send(ctl, "ramen in Boston")
    options = ctl.backend.calls[0]["options"]
    assert options.web is False
    assert options.ephemeral is True
    assert options.schema["required"] == ["what", "where", "on_topic"]
    assert ctl.backend.calls[0]["cwd"] == store.scratch_dir()


# AUDIENCE --------------------------------------------------------------------------------


async def at_audience(store, collectors=None) -> Controller:
    ctl = controller(store, {"ramen in Boston": reply("ramen", "Boston")}, collectors)
    await send(ctl, "ramen in Boston")
    assert ctl.state is S.AUDIENCE
    return ctl


async def test_bad_audience_answer_keeps_state(store):
    ctl = await at_audience(store)
    assert await send(ctl, "7") == [Say(HINTS[S.AUDIENCE], "muted")]
    assert ctl.state is S.AUDIENCE


async def test_audience_choice_runs_collectors_and_reaches_research(store):
    places = FakeCollector(places_result(n=7, reviewed=5), "Google Places")
    skipped = CollectorResult(source="youtube", status="skipped", note="no key")
    youtube = FakeCollector(skipped, "YouTube", available=False)
    ctl = await at_audience(store, [places, youtube])

    events = await send(ctl, "2")

    steps = [e for e in events if isinstance(e, Step)]
    assert [(s.status, s.text) for s in steps[:3]] == [
        ("ok", "Found 7 competitors (Google Places)"),
        ("ok", "Pulled reviews for 5 places"),
        ("skip", "YouTube skipped (no key)"),
    ]
    assert steps[3].text.startswith("Researching")
    assert ctl.state is S.RESEARCHING
    saved = store.load(ctl.session.id)
    assert saved.state is S.RESEARCHING
    assert saved.intake.audience is Audience.EXISTING
    assert [r.source for r in saved.collectors] == ["google_places", "youtube"]
    assert "https://maps.google.com/?cid=0" in saved.observed_urls


async def test_enter_skips_the_audience(store):
    ctl = await at_audience(store)
    await send(ctl, "")
    assert ctl.session.intake.audience is None
    assert ctl.state is S.RESEARCHING


async def test_collector_errors_show_as_skipped(store):
    broken = FakeCollector(
        CollectorResult(source="google_places", status="error", note="invalid key"),
        "Google Places",
    )
    ctl = await at_audience(store, [broken])
    events = await send(ctl, "1")
    assert Step("Google Places skipped (invalid key)", "skip") in events


async def test_enter_after_an_interruption_continues_without_recollecting(store):
    places = FakeCollector(places_result(), "Google Places")
    ctl = await at_audience(store, [places])
    await send(ctl, "1")
    assert ctl.state is S.RESEARCHING
    await send(ctl, "")  # e.g. after Ctrl-C during research
    assert places.calls == 1
    assert await send(ctl, "more text") == [Say(HINTS[S.RESEARCHING], "muted")]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", Audience.NEW_OPENING),
        ("[2]", Audience.EXISTING),
        ("creator", Audience.CREATOR),
        ("Chain adding a location", Audience.CHAIN),
        ("9", None),
        ("pizza", None),
    ],
)
def test_parse_audience(answer, expected):
    assert parse_audience(answer) is expected


# Commands --------------------------------------------------------------------------------


async def test_report_and_sources_before_a_brief(store):
    ctl = controller(store)
    assert await send(ctl, "/report") == [Say(NOTHING_YET, "muted")]
    ctl = await at_audience(store)
    assert await send(ctl, "/sources") == [Say(NOTHING_YET, "muted")]
    assert ctl.state is S.AUDIENCE


async def test_help_lists_commands(store):
    [event] = await send(controller(store), "/help")
    for command in ("/report", "/sources", "/exit", "/help"):
        assert command in event.text


async def test_unknown_command(store):
    [event] = await send(controller(store), "/dance")
    assert "Unknown command /dance" in event.text


async def test_exit_before_a_session_just_ends(store):
    ctl = controller(store)
    await send(ctl, "/exit")
    assert ctl.state is S.ENDED
    assert await send(ctl, "anything") == [Say(HINTS[S.ENDED], "muted")]


async def test_exit_saves_and_keeps_the_resumable_state(store):
    ctl = await at_audience(store)
    events = await send(ctl, "/exit")
    assert ctl.state is S.ENDED
    assert f"milo resume {ctl.session.id}" in texts(events)[0]
    assert store.load(ctl.session.id).state is S.AUDIENCE


async def test_resumed_session_opens_where_it_left_off(store):
    ctl = await at_audience(store)
    await send(ctl, "/exit")
    resumed = Controller(
        backend=FakeBackend(), collectors=[], store=store, session=store.load(ctl.session.id)
    )
    assert resumed.state is S.AUDIENCE
    assert resumed.opening()[-1] == Ask(AUDIENCE_QUESTION)
