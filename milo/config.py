"""Optional API keys: loading, env overrides, and a private config file."""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomli_w

KeySource = Literal["env", "file"]


@dataclass(frozen=True)
class KeySpec:
    name: str  # key in config.toml
    env_var: str  # overrides the file when set
    label: str  # shown to users


PLACES = KeySpec("google_places_api_key", "MILO_PLACES_KEY", "Google Places")
YOUTUBE = KeySpec("youtube_api_key", "MILO_YOUTUBE_KEY", "YouTube")
KEYS = (PLACES, YOUTUBE)


class ConfigError(Exception):
    """The config file exists but can't be read."""


@dataclass(frozen=True)
class Config:
    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, KeySource] = field(default_factory=dict)
    problem: str | None = None  # why the file was ignored, if it was

    def key(self, spec: KeySpec) -> str | None:
        return self.values.get(spec.name)

    def source(self, spec: KeySpec) -> KeySource | None:
        return self.sources.get(spec.name)


def config_dir() -> Path:
    return Path.home() / ".config" / "milo"


def config_path() -> Path:
    return config_dir() / "config.toml"


def mask(key: str | None) -> str:
    """Show just enough of a key to recognize it: `AIza…x9Q`."""
    if not key:
        return "–"
    if len(key) < 12:
        return "•••"
    return f"{key[:4]}…{key[-3:]}"


def load(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Resolve every key from env vars first, then the config file. Never raises."""
    path = path or config_path()
    env = os.environ if env is None else env
    problem = None
    try:
        file_values = _read(path)
    except ConfigError as exc:
        file_values, problem = {}, str(exc)

    values: dict[str, str] = {}
    sources: dict[str, KeySource] = {}
    for spec in KEYS:
        from_env = env.get(spec.env_var, "").strip()
        from_file = file_values.get(spec.name)
        if from_env:
            values[spec.name], sources[spec.name] = from_env, "env"
        elif isinstance(from_file, str) and from_file.strip():
            values[spec.name], sources[spec.name] = from_file.strip(), "file"
    return Config(values, sources, problem)


def save(updates: Mapping[str, str], path: Path | None = None) -> Path:
    """Merge `updates` into the config file, keeping it readable only by the user."""
    path = path or config_path()
    try:
        data = _read(path)
    except ConfigError:
        data = {}  # unreadable file: replace it rather than refuse to save
    data.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        tomli_w.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def file_mode(path: Path) -> int | None:
    """Permission bits of `path`, or None if it doesn't exist."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def is_private(mode: int) -> bool:
    return mode & 0o077 == 0


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} isn't valid TOML ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"can't read {path} ({exc.strerror})") from exc
