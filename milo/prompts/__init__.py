"""Prompt templates (Jinja). The flow lives in Python; these only carry instructions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=False,  # plain-text prompts, not HTML
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render(name: str, **context: Any) -> str:
    return _env.get_template(name).render(**context)
