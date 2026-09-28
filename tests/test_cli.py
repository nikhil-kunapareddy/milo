import httpx
import pytest
from typer.testing import CliRunner

from milo import __version__, cli, config
from milo.backends.claude_code import CliStatus
from milo.collectors import places, youtube
from tests.conftest import fixture_json

runner = CliRunner()

PLACES_KEY = "AIzaSyPLACESPLACESPLACESPLACESPLACES9aZ"
YOUTUBE_KEY = "AIzaSyYOUTUBEYOUTUBEYOUTUBEYOUTUBEYOU7bQ"
YOUTUBE_LANGUAGES = f"{youtube.API_ROOT}/i18nLanguages"


@pytest.fixture
def claude_ready(monkeypatch):
    async def ready() -> CliStatus:
        return CliStatus(installed=True, logged_in=True, version="2.1.283", auth_method="claude.ai")

    monkeypatch.setattr(cli, "check_cli", ready)


@pytest.fixture
def secrets(monkeypatch):
    """Feed answers to the hidden key prompt, in order."""

    def feed(*answers: str) -> None:
        pending = list(answers)
        monkeypatch.setattr(cli, "ask_secret", lambda label: pending.pop(0))

    return feed


def places_accepts_only(key: str):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers["X-Goog-Api-Key"] == key:
            return httpx.Response(200, json={"places": []})
        return httpx.Response(400, json=fixture_json("places/error_invalid_key.json"))

    return respond


def test_version():
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"milo {__version__}"


def test_doctor_without_keys(claude_ready):
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "✓ 2.1.283 · logged in (claude.ai)" in result.output
    assert result.output.count("not configured") == 2
    assert "not created yet" in result.output


def test_doctor_with_keys_masks_them(claude_ready, http, monkeypatch):
    config.save({"google_places_api_key": PLACES_KEY})
    monkeypatch.setenv("MILO_YOUTUBE_KEY", YOUTUBE_KEY)
    http.post(places.TEXT_SEARCH_URL).respond(200, json={"places": []})
    http.get(YOUTUBE_LANGUAGES).respond(403, json=fixture_json("youtube/error_quota.json"))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "✓ works · AIza…9aZ (config file)" in result.output
    assert "! quota exceeded · AIza…7bQ (from MILO_YOUTUBE_KEY)" in result.output
    assert "(0600)" in result.output
    assert PLACES_KEY not in result.output and YOUTUBE_KEY not in result.output


def test_doctor_reports_an_invalid_key(claude_ready, http):
    config.save({"google_places_api_key": PLACES_KEY})
    http.post(places.TEXT_SEARCH_URL).respond(
        400, json=fixture_json("places/error_invalid_key.json")
    )
    result = runner.invoke(cli.app, ["doctor"])
    assert "✗ invalid key · AIza…9aZ" in result.output


def test_doctor_warns_about_loose_permissions(claude_ready, http):
    path = config.save({"google_places_api_key": PLACES_KEY})
    path.chmod(0o644)
    http.post(places.TEXT_SEARCH_URL).respond(200, json={})
    result = runner.invoke(cli.app, ["doctor"])
    assert "readable by others (0644)" in result.output


def test_doctor_fails_without_claude_code():
    result = runner.invoke(cli.app, ["doctor"])  # conftest keeps the real `claude` off PATH
    assert result.exit_code == 1
    assert "Claude Code isn't installed" in result.output


def test_setup_retries_a_bad_key_then_saves_a_good_one(http, secrets):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=places_accepts_only(PLACES_KEY))
    secrets("AIzaSyWRONGWRONGWRONGWRONGWRONGWRONGWR0", PLACES_KEY, "")

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0, result.output
    assert "✗ invalid key. Try again" in result.output
    assert "✓ AIza…9aZ works" in result.output
    assert "– skipped" in result.output  # YouTube
    cfg = config.load()
    assert cfg.key(config.PLACES) == PLACES_KEY
    assert cfg.key(config.YOUTUBE) is None
    assert config.file_mode(config.config_path()) == 0o600
    assert PLACES_KEY not in result.output


def test_setup_skipping_everything_writes_nothing(secrets):
    secrets("", "")
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert "No changes saved." in result.output
    assert config.file_mode(config.config_path()) is None


def test_setup_keeps_existing_key_on_enter(secrets):
    config.save({"youtube_api_key": YOUTUBE_KEY})
    secrets("", "")
    result = runner.invoke(cli.app, ["setup"])
    assert "Current: AIza…7bQ (config file)" in result.output
    assert "kept the current key" in result.output
    assert config.load().key(config.YOUTUBE) == YOUTUBE_KEY


def test_setup_offline_can_save_unchecked_key(http, secrets):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ConnectError("offline"))
    secrets(PLACES_KEY, "")
    result = runner.invoke(cli.app, ["setup"], input="y\n")
    assert "Couldn't check AIza…9aZ (network error)" in result.output
    assert config.load().key(config.PLACES) == PLACES_KEY


def test_setup_offline_declined_does_not_save(http, secrets):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ConnectError("offline"))
    secrets(PLACES_KEY, "", "")
    result = runner.invoke(cli.app, ["setup"], input="n\n")
    assert result.exit_code == 0, result.output
    assert config.load().key(config.PLACES) is None
