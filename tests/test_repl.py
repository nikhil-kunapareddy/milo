"""The interactive `milo` loop, end to end through the CLI with a fake backend."""

import pytest
from typer.testing import CliRunner

from milo import cli
from milo.backends.base import BackendStatus
from milo.session import OFF_TOPIC
from milo.store import Store
from tests.fakes import FakeBackend, intake_replies, reply

runner = CliRunner()


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend(
        intake_replies(
            {
                "write me a python web scraper": reply(None, None, on_topic=False),
                "I run a ramen place near Fenway, want more students": reply(
                    "ramen", "Fenway, Boston"
                ),
            }
        )
    )
    monkeypatch.setattr(cli, "get_backend", lambda name: fake)
    return fake


def run(*lines: str):
    return runner.invoke(cli.app, [], input="".join(f"{line}\n" for line in lines))


def test_off_topic_then_valid_request_reaches_research(backend, home):
    result = run(
        "write me a python web scraper",
        "I run a ramen place near Fenway, want more students",
        "2",
    )
    out = result.output

    assert result.exit_code == 0, out
    assert "Milo · marketing research for food businesses" in out
    assert (
        "Backend: Fake Backend ✓ · Google Places – (not configured) · YouTube – (not configured)"
        in out
    )
    assert OFF_TOPIC in out
    assert (
        "[1] New opening  [2] Existing restaurant  [3] Creator  [4] Chain adding a location" in out
    )
    assert "– Google Places skipped (no key)" in out
    assert "– YouTube skipped (no key)" in out
    assert "→ Researching local marketing and best practices…" in out
    assert "Saved session ramen-fenway-boston-" in out  # stdin ran out: treated as /exit

    assert "Market snapshot" in out
    assert "Competitors" in out and "Tora Ramen" in out
    assert "┃ Rating" not in out  # no Places data: the empty number columns are hidden
    assert "need a Google Places key" in out
    assert "Campaign ideas" in out and "1. Student lunch bowl" in out
    assert "7-day content calendar" in out
    assert "Sources" in out

    saved = Store().latest()
    assert saved.state == "follow_up"
    assert saved.intake.audience == "existing_restaurant"


def test_commands_before_the_brief(backend):
    result = run("/sources", "/help", "/exit")
    assert "Nothing yet" in result.output
    assert "/report" in result.output and "save the session and quit" in result.output


def test_backend_not_ready_stops_before_asking(monkeypatch):
    status = BackendStatus(installed=False, problem="Claude Code isn't installed. Install it.")
    monkeypatch.setattr(cli, "get_backend", lambda name: FakeBackend(status=status))
    result = run("ramen in Boston")
    assert result.exit_code == 1
    assert "Backend: Fake Backend ✗" in result.output
    assert "Claude Code isn't installed" in result.output
    assert "What do you want to market" not in result.output


def test_codex_backend_says_coming_soon():
    result = runner.invoke(cli.app, ["--backend", "codex"], input="")
    assert result.exit_code == 1
    assert "Codex backend coming soon — use Claude Code for now" in result.output


def test_unknown_backend_is_rejected():
    result = runner.invoke(cli.app, ["--backend", "gpt"], input="")
    assert result.exit_code != 0
    assert "choose one of" in result.output


def test_brief_with_places_numbers_shows_them():
    from milo.models import Brief
    from tests.fakes import brief_data

    brief = Brief.model_validate(brief_data())  # the fixture's competitors carry numbers
    with cli.console.capture() as captured:
        cli._render_brief(brief)
    out = captured.get()
    assert "┃ Rating" in out and "4.9" in out and "99,999" in out
    assert "need a Google Places key" not in out
    assert " 1. Source 1" in out


# milo resume ------------------------------------------------------------------------------


def test_resume_with_no_sessions():
    result = runner.invoke(cli.app, ["resume"])
    assert result.exit_code == 1
    assert "No saved sessions yet" in result.output


def test_resume_unknown_session():
    result = runner.invoke(cli.app, ["resume", "nope-nope-0000"])
    assert result.exit_code == 1
    assert "No saved session called 'nope-nope-0000'" in result.output


def test_resume_lands_in_follow_up_and_reports(monkeypatch, tmp_path):
    from tests.fakes import scripted
    from tests.test_followup import follow_up_session

    follow_up_session(Store())
    fake = FakeBackend(scripted({}, follow_up=["Try a **student lunch** deal."]))
    chosen = []
    monkeypatch.setattr(cli, "get_backend", lambda name: chosen.append(name) or fake)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app, ["resume"], input="what should I try first?\n/sources\n/report\n/exit\n"
    )

    out = result.output
    assert result.exit_code == 0, out
    assert chosen == ["claude-code"]  # the session's own backend
    assert "Resumed ramen-boston-7f3a: ramen in Boston" in out
    assert "Market snapshot" in out  # the brief is shown again
    assert "Try a student lunch deal." in out
    assert out.count("Boston Magazine") >= 2  # in the brief's sources and in /sources
    assert "Saved ./milo-ramen-boston-" in out
    [written] = tmp_path.glob("milo-ramen-boston-*.md")
    assert "### Q: what should I try first?" in written.read_text()
    assert "Saved session ramen-boston-7f3a" in out
