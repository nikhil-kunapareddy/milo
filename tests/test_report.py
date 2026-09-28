"""Markdown report rendering, straight from a stored session."""

from datetime import date

import pytest

from milo import report
from milo.models import (
    Audience,
    Brief,
    CollectorResult,
    Intake,
    Session,
    SessionState,
    Source,
    Turn,
)
from milo.store import now
from tests.fakes import brief_data

DAY = date(2026, 9, 27)
SOURCES = [
    Source(id=1, title="Boston Magazine [2026]", url="https://www.bostonmagazine.com/ramen/"),
    Source(id=2, title="Patch", url="https://patch.com/ramen-spot"),
]


def make_session(**overrides) -> Session:
    brief = Brief.model_validate(brief_data(sources=[s.model_dump() for s in SOURCES]))
    for c in brief.competitors:
        c.rating = c.review_count = c.price_level = None
    fields = {
        "id": "ramen-boston-7f3a",
        "created": now(),
        "updated": now(),
        "state": SessionState.FOLLOW_UP,
        "intake": Intake(what="ramen", where="Fenway, Boston", audience=Audience.EXISTING),
        "backend": "claude-code",
        "collectors": [
            CollectorResult(source="google_places", status="error", note="quota exceeded"),
            CollectorResult(source="youtube", status="skipped", note="no key"),
        ],
        "brief": brief,
        "sources": SOURCES,
        "turns": [
            Turn(
                question="which competitor has the weakest social presence?",
                answer="## Short answer\nRamen Spot, per [Patch](https://patch.com/ramen-spot).",
                asked_at=now(),
            )
        ],
    }
    return Session(**{**fields, **overrides})


@pytest.fixture
def text() -> str:
    return report.render(make_session(), DAY)


def test_filename():
    assert report.filename(make_session(), DAY) == "milo-ramen-fenway-boston-2026-09-27.md"


def test_sections_appear_in_order(text):
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Market snapshot",
        "## Competitors",
        "## Review themes",
        "## Gaps and opportunities",
        "## Campaign ideas",
        "## 7-day content calendar",
        "## Follow-up Q&A",
        "## Data coverage",
        "## Sources",
    ]  # content benchmarks are omitted because the brief has none


def test_title_and_details(text):
    assert text.startswith("# Milo brief: ramen in Fenway, Boston\n")
    assert "**Audience:** Existing restaurant · **Date:** 2026-09-27" in text


def test_benchmarks_appear_when_present():
    session = make_session()
    session.brief.content_benchmarks = ["Reaction clips beat static posts"]
    assert "## Content benchmarks\n\n- Reaction clips beat static posts" in report.render(
        session, DAY
    )


def test_competitors_without_places_numbers(text):
    assert "| Name | Positioning | Sources |" in text
    assert "| Tora Ramen | Chinatown favorite for rich tonkotsu | [10], [1] |" in text
    assert "only from Google Places" in text


def test_competitors_with_places_numbers():
    session = make_session()
    session.brief.competitors[0].rating = 4.5
    session.brief.competitors[0].review_count = 1287
    session.brief.competitors[0].price_level = "$$"
    text = report.render(session, DAY)
    assert "| Name | Rating | Reviews | Price | Positioning | Sources |" in text
    assert "| Tora Ramen | 4.5 | 1,287 | $$ |" in text
    assert "| Santouka | – | – | – |" in text


def test_table_cells_are_escaped():
    session = make_session()
    session.brief.competitors[0].positioning = "Cheap | fast\nand loud"
    assert "Cheap \\| fast and loud" in report.render(session, DAY)


def test_campaign_ideas_and_calendar(text):
    assert "### 1. Student lunch bowl" in text
    assert "**Why it fits:** Reviews call $19 steep [10], [11], [12]" in text
    assert "| Mon | Instagram | Mon post |" in text


def test_follow_ups_are_included_with_headings_demoted(text):
    assert "### Q: which competitor has the weakest social presence?" in text
    assert "##### Short answer" in text


def test_data_coverage(text):
    assert (
        "Competitor ratings and reviews: skipped (Google Places quota exceeded)"
        " · Video benchmarks: skipped (no YouTube key)"
        " · Web research: Claude Code ✓ (2 verified sources)"
    ) in text


def test_coverage_when_everything_worked():
    session = make_session(
        collectors=[
            CollectorResult(source="google_places", status="ok", data={"places": []}),
            CollectorResult(source="youtube", status="ok", data={"videos": []}),
        ]
    )
    assert report.coverage(session)[:2] == [
        "Competitor ratings and reviews: Google Places ✓",
        "Video benchmarks: YouTube ✓",
    ]


def test_sources_are_numbered_and_linkable(text):
    assert "1. [Boston Magazine \\[2026\\]](https://www.bostonmagazine.com/ramen/)" in text
    assert "2. [Patch](https://patch.com/ramen-spot)" in text
    assert text.rstrip().endswith("[2]: https://patch.com/ramen-spot")


def test_only_the_sessions_verified_sources_are_listed():
    # The brief's own list is ignored; the report uses session.sources (verified only).
    session = make_session(sources=[SOURCES[1]])
    text = report.render(session, DAY)
    assert "bostonmagazine.com/ramen" not in text.split("## Sources")[1]


def test_raw_brief_report():
    session = make_session(brief=None, brief_raw="# Findings\nRamen is hot.", turns=[])
    text = report.render(session, DAY)
    assert "## Brief" in text and "#### Findings" in text
    assert "## Market snapshot" not in text and "## Follow-up Q&A" not in text


def test_write_never_overwrites(tmp_path):
    session = make_session()
    paths = [report.write(session, tmp_path, DAY) for _ in range(3)]
    assert [p.name for p in paths] == [
        "milo-ramen-fenway-boston-2026-09-27.md",
        "milo-ramen-fenway-boston-2026-09-27-2.md",
        "milo-ramen-fenway-boston-2026-09-27-3.md",
    ]
    (tmp_path / "milo-other.md").write_text("mine")
    assert (tmp_path / "milo-other.md").read_text() == "mine"


def test_existing_file_is_left_alone(tmp_path):
    taken = tmp_path / "milo-ramen-fenway-boston-2026-09-27.md"
    taken.write_text("my notes")
    path = report.write(make_session(), tmp_path, DAY)
    assert taken.read_text() == "my notes"
    assert path.name.endswith("-2.md")
