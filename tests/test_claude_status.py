from milo.backends.claude_code import check_cli
from tests.conftest import write_executable

FAKE_CLAUDE = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "2.1.283 (Claude Code)"; exit 0; fi
if [ "$1" = "auth" ]; then echo '{auth}'; exit 0; fi
exit 1
"""


def fake_claude(bin_dir, auth_output: str) -> None:
    write_executable(bin_dir / "claude", FAKE_CLAUDE.replace("{auth}", auth_output))


async def test_missing_cli_explains_how_to_install(fake_bin):
    status = await check_cli()
    assert not status.installed and not status.ready
    assert "install.sh" in status.problem


async def test_logged_in_cli_is_ready(fake_bin):
    fake_claude(fake_bin, '{"loggedIn": true, "authMethod": "claude.ai"}')
    status = await check_cli()
    assert status.ready
    assert (status.version, status.auth_method) == ("2.1.283", "claude.ai")


async def test_logged_out_cli_explains_how_to_log_in(fake_bin):
    fake_claude(fake_bin, '{"loggedIn": false, "authMethod": "none"}')
    status = await check_cli()
    assert status.installed and status.logged_in is False and not status.ready
    assert "claude auth login" in status.problem


async def test_unreadable_auth_status_is_not_treated_as_logged_in(fake_bin):
    fake_claude(fake_bin, "unknown command: auth")
    status = await check_cli()
    assert status.installed and status.logged_in is None and not status.ready
    assert "claude update" in status.problem
