"""Shared test setup: a fake home, no real network, and no real `claude` on PATH."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"
MILO_ENV_VARS = ("MILO_PLACES_KEY", "MILO_YOUTUBE_KEY")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a temp dir so tests never touch the real ~/.config or ~/.milo."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    for var in MILO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLUMNS", "200")  # keep Rich from wrapping lines mid-assertion
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return fake_home


@pytest.fixture(autouse=True)
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH without the real `claude`. Tests drop fake executables in here."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


@pytest.fixture(autouse=True)
def http():
    """Every test runs with HTTP mocked; an unmocked request fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def fixture_json(relative: str) -> dict:
    return json.loads((FIXTURES / relative).read_text())


def write_executable(path: Path, script: str) -> Path:
    path.write_text(script)
    path.chmod(0o755)
    return path
