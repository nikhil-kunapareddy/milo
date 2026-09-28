"""The Claude Code adapter, driven against a fake `claude` script (never the real CLI)."""

import time
from contextlib import aclosing

import pytest

from milo.backends import get_backend
from milo.backends.base import Error, Final, RunOptions, SessionStarted, ToolCall
from milo.backends.claude_code import ClaudeCodeBackend, build_command
from milo.backends.codex import NOT_READY, CodexBackend
from tests.conftest import FIXTURES, write_executable

RECORDING_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_OUT/args"
pwd > "$FAKE_OUT/cwd"
cat > "$FAKE_OUT/stdin"
env > "$FAKE_OUT/env"
cat "$FAKE_FIXTURE"
"""


@pytest.fixture
def recorder(fake_bin, tmp_path, monkeypatch):
    out = tmp_path / "recorded"
    out.mkdir()
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("FAKE_FIXTURE", str(FIXTURES / "stream" / "research_search_fetch.jsonl"))
    write_executable(fake_bin / "claude", RECORDING_CLAUDE)
    return out


async def collect(backend, prompt="hi", **kwargs) -> list:
    async with aclosing(backend.run(prompt, **kwargs)) as stream:
        return [event async for event in stream]


async def test_streams_events_from_the_cli(recorder, tmp_path):
    events = await collect(ClaudeCodeBackend(), cwd=tmp_path / "work")
    assert isinstance(events[0], SessionStarted)
    assert [e.name for e in events if isinstance(e, ToolCall)] == ["WebSearch", "WebFetch"]
    assert isinstance(events[-1], Final)


async def test_runs_in_the_session_folder_with_prompt_on_stdin(recorder, tmp_path):
    work = tmp_path / "sessions" / "ramen-boston-7f3a" / "work"
    await collect(ClaudeCodeBackend(), "What sells ramen to students?", cwd=work)
    assert work.is_dir()
    assert (recorder / "cwd").read_text().strip() == str(work.resolve())
    assert (recorder / "stdin").read_text() == "What sells ramen to students?"
    assert "What sells ramen" not in (recorder / "args").read_text()


async def test_milo_keys_never_reach_the_agent(recorder, tmp_path, monkeypatch):
    monkeypatch.setenv("MILO_PLACES_KEY", "AIzaSECRETSECRETSECRET")
    monkeypatch.setenv("MILO_YOUTUBE_KEY", "AIzaSECRET2SECRET2")
    await collect(ClaudeCodeBackend(), cwd=tmp_path / "work")
    child_env = (recorder / "env").read_text()
    assert "SECRET" not in child_env
    assert "FAKE_OUT=" in child_env  # everything else passes through


async def test_resume_and_options_reach_the_command(recorder, tmp_path):
    options = RunOptions(web=False, schema={"type": "object"}, ephemeral=True, max_turns=2)
    await collect(ClaudeCodeBackend(), cwd=tmp_path / "w", resume="abc-123", options=options)
    args = (recorder / "args").read_text().splitlines()
    assert args[args.index("--resume") + 1] == "abc-123"
    assert args[args.index("--json-schema") + 1] == '{"type": "object"}'
    assert args[args.index("--max-turns") + 1] == "2"
    assert "--no-session-persistence" in args


async def test_not_installed(tmp_path):
    [event] = await collect(ClaudeCodeBackend(), cwd=tmp_path / "work")
    assert isinstance(event, Error) and event.kind == "not_installed"
    assert "install.sh" in event.message


async def test_crash_reports_stderr(fake_bin, tmp_path):
    write_executable(fake_bin / "claude", "#!/bin/sh\necho 'boom: config broken' >&2\nexit 3\n")
    [event] = await collect(ClaudeCodeBackend(), cwd=tmp_path / "work")
    assert isinstance(event, Error) and event.kind == "crashed"
    assert "boom: config broken" in event.message


async def test_stopping_early_kills_the_process(fake_bin, tmp_path):
    first_line = (FIXTURES / "stream" / "search_only.jsonl").read_text().splitlines()[0]
    (tmp_path / "first.jsonl").write_text(first_line + "\n")
    script = f"#!/bin/sh\ncat > /dev/null\ncat {tmp_path / 'first.jsonl'}\nsleep 30\n"
    write_executable(fake_bin / "claude", script)

    start = time.monotonic()
    async with aclosing(ClaudeCodeBackend().run("hi", cwd=tmp_path / "work")) as stream:
        async for event in stream:
            assert isinstance(event, SessionStarted)
            break
    assert time.monotonic() - start < 2


async def test_huge_stream_lines_are_fine(fake_bin, tmp_path):
    big = '{"type": "rate_limit_event", "padding": "' + "x" * 200_000 + '"}'
    final = '{"type": "result", "subtype": "success", "is_error": false, "result": "done"}'
    (tmp_path / "big.jsonl").write_text(f"{big}\n{final}\n")
    write_executable(fake_bin / "claude", f"#!/bin/sh\ncat > /dev/null\ncat {tmp_path}/big.jsonl\n")
    [event] = await collect(ClaudeCodeBackend(), cwd=tmp_path / "work")
    assert isinstance(event, Final) and event.text == "done"


def test_command_is_locked_down():
    command = build_command("claude", resume=None, options=RunOptions())
    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--safe-mode" in command
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert command[command.index("--tools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    forbidden = {"--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
    assert not forbidden & set(command)
    assert "--resume" not in command and "--json-schema" not in command


def test_no_web_means_no_tools_at_all():
    command = build_command("claude", resume=None, options=RunOptions(web=False))
    assert command[command.index("--tools") + 1] == ""
    assert "--allowedTools" not in command


def test_codex_is_a_stub(fake_bin, tmp_path):
    backend = CodexBackend()
    assert not backend.available()
    write_executable(fake_bin / "codex", "#!/bin/sh\n")
    assert backend.available()
    with pytest.raises(NotImplementedError, match="coming soon"):
        backend.run("hi", cwd=tmp_path)
    assert "use Claude Code" in NOT_READY


def test_claude_code_is_the_default_backend():
    assert get_backend().name == "claude-code"
    assert get_backend("codex").name == "codex"
    with pytest.raises(KeyError):
        get_backend("gpt")


async def test_events_arrive_as_they_stream(fake_bin, tmp_path):
    """Progress must reach the user while the CLI is still running, not at the end."""
    lines = (FIXTURES / "stream" / "search_only.jsonl").read_text().splitlines()
    (tmp_path / "head.jsonl").write_text(lines[0] + "\n")
    (tmp_path / "tail.jsonl").write_text("\n".join(lines[1:]) + "\n")
    script = (
        f"#!/bin/sh\ncat > /dev/null\ncat {tmp_path}/head.jsonl\n"
        f"sleep 1\ncat {tmp_path}/tail.jsonl\n"
    )
    write_executable(fake_bin / "claude", script)

    start = time.monotonic()
    arrivals = []
    async with aclosing(ClaudeCodeBackend().run("hi", cwd=tmp_path / "work")) as stream:
        async for event in stream:
            arrivals.append((type(event).__name__, time.monotonic() - start))
    assert arrivals[0][0] == "SessionStarted" and arrivals[0][1] < 0.8
    assert arrivals[-1][0] == "Final" and arrivals[-1][1] >= 1.0
