"""Markdown report, rendered from the stored session. The model is never asked to write it."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from milo.backends import BACKENDS
from milo.collectors import LABELS
from milo.models import Session
from milo.store import slugify

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,  # Markdown, not HTML
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters.update(
    cell=lambda text: str(text).replace("|", "\\|").replace("\n", " "),
    oneline=lambda text: " ".join(str(text).split()),
    link_text=lambda text: str(text).replace("[", "\\[").replace("]", "\\]"),
    # "[1], [3]": each is a reference link to its source (see the definitions at the bottom).
    refs=lambda ids: ", ".join(f"[{i}]" for i in ids) or "–",
    rating=lambda value: f"{value:.1f}" if value is not None else "–",
    count=lambda value: f"{value:,}" if value is not None else "–",
    # Model-written Markdown sits under a "###" heading, so push its own headings below that.
    demote=lambda text: re.sub(
        r"^(#{1,6})(?=\s)", lambda m: "#" * min(6, len(m.group(1)) + 3), str(text), flags=re.M
    ),
)

COVERAGE = (
    ("google_places", "Competitor ratings and reviews"),
    ("youtube", "Video benchmarks"),
)


def filename(session: Session, day: date) -> str:
    what, where = slugify(session.intake.what), slugify(session.intake.where)
    return f"milo-{what}-{where}-{day.isoformat()}.md"


def coverage(session: Session) -> list[str]:
    """One line per data source, e.g. "Video benchmarks: skipped (no YouTube key)"."""
    results = {r.source: r for r in session.collectors}
    lines = []
    for source, what in COVERAGE:
        label = LABELS[source]
        result = results.get(source)
        if result is None:
            lines.append(f"{what}: not collected")
        elif result.status == "ok":
            lines.append(f"{what}: {label} ✓")
        else:
            note = f"no {label} key" if result.note == "no key" else f"{label} {result.note}"
            lines.append(f"{what}: skipped ({note})")
    backend = BACKENDS[session.backend].label if session.backend in BACKENDS else session.backend
    lines.append(f"Web research: {backend} ✓ ({len(session.sources)} verified sources)")
    return lines


def render(session: Session, day: date) -> str:
    brief = session.brief
    numbers = brief is not None and any(
        c.rating is not None or c.review_count is not None or c.price_level
        for c in brief.competitors
    )
    audience = session.intake.audience
    return _env.get_template("report.md.j2").render(
        intake=session.intake,
        audience=audience.label if audience else None,
        day=day.isoformat(),
        brief=brief,
        brief_raw=session.brief_raw or "",
        numbers=numbers,
        turns=session.turns,
        coverage=coverage(session),
        sources=session.sources,
    )


def write(session: Session, directory: Path, day: date | None = None) -> Path:
    """Write the report without ever overwriting: name.md, then name-2.md, name-3.md, ..."""
    day = day or date.today()
    text = render(session, day)
    base = filename(session, day).removesuffix(".md")
    for n in range(1, 1000):
        path = directory / (f"{base}.md" if n == 1 else f"{base}-{n}.md")
        try:
            with path.open("x", encoding="utf-8") as f:  # "x" fails if the file exists
                f.write(text)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"Too many reports named {base} in {directory}")
