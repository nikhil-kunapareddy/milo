from milo.backends.base import Error, Final
from milo.intake import IntakeFailure, IntakeParse, parse_intake, parse_reply
from tests.fakes import FakeBackend


def backend_replying(*events):
    return FakeBackend(lambda prompt, options, resume: list(events))


async def test_structured_output_is_preferred(tmp_path):
    structured = {"what": "ramen", "where": "Fenway, Boston", "on_topic": True}
    backend = backend_replying(Final("ignored text", "s", structured=structured))
    result = await parse_intake(backend, "ramen near Fenway", cwd=tmp_path)
    assert result == IntakeParse(what="ramen", where="Fenway, Boston", on_topic=True)


async def test_text_reply_is_parsed_when_there_is_no_structured_output(tmp_path):
    text = '```json\n{"what": "Pho", "where": "Denver", "on_topic": true}\n```'
    result = await parse_intake(backend_replying(Final(text, "s")), "pho in denver", cwd=tmp_path)
    assert (result.what, result.where) == ("pho", "Denver")


async def test_the_message_is_embedded_in_the_prompt(tmp_path):
    backend = backend_replying(Final("{}", "s"))
    await parse_intake(backend, "I sell empanadas in Miami", cwd=tmp_path)
    assert "<<<\nI sell empanadas in Miami\n>>>" in backend.calls[0]["prompt"]


async def test_unreadable_reply_returns_none(tmp_path):
    assert await parse_intake(backend_replying(Final("no idea", "s")), "x", cwd=tmp_path) is None


async def test_no_final_event_returns_none(tmp_path):
    assert await parse_intake(backend_replying(), "x", cwd=tmp_path) is None


async def test_login_problems_are_surfaced(tmp_path):
    error = Error("not_logged_in", "log in please")
    result = await parse_intake(backend_replying(error), "x", cwd=tmp_path)
    assert result == IntakeFailure("log in please")


async def test_other_errors_fall_back(tmp_path):
    result = await parse_intake(backend_replying(Error("max_turns", "too long")), "x", cwd=tmp_path)
    assert result is None


def test_blank_fields_count_as_missing():
    parsed = parse_reply({"what": "  ", "where": "", "on_topic": True})
    assert (parsed.what, parsed.where) == (None, None)


def test_wrong_types_are_rejected():
    assert parse_reply({"what": ["ramen"], "where": "Boston", "on_topic": True}) is None
    assert parse_reply({"what": "ramen", "where": "Boston"}) is None  # on_topic missing


def test_prose_around_json_is_tolerated():
    parsed = parse_reply('Here you go: {"what": "tacos", "where": null, "on_topic": true}. Enjoy!')
    assert (parsed.what, parsed.where) == ("tacos", None)


def test_overlong_fields_are_trimmed():
    parsed = parse_reply({"what": "x" * 500, "where": "y" * 500, "on_topic": True})
    assert len(parsed.what) == 60 and len(parsed.where) == 80
