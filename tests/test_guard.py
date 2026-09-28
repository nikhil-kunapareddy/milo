"""Citation guard: nothing unverified reaches the user."""

import json

import pytest

from milo.backends.base import ToolResult
from milo.backends.stream import StreamParser
from milo.guard import UNVERIFIED, CitationGuard, ground_competitors, normalize
from milo.models import Brief
from tests.conftest import FIXTURES, fixture_json

MADE_UP = "https://www.bostonglobe.com/2026/09/01/food/fenway-ramen-war-invented/"
BOSTON_MAG = "https://www.bostonmagazine.com/restaurants/best-ramen-in-boston/"


def observed_from_stream(name: str) -> list[str]:
    parser = StreamParser()
    urls: list[str] = []
    for line in (FIXTURES / "stream" / name).read_text().splitlines():
        urls += [u for e in parser.feed(line) if isinstance(e, ToolResult) for u in e.urls]
    return urls


@pytest.fixture
def guard() -> CitationGuard:
    return CitationGuard(json.loads((FIXTURES / "briefs" / "observed_urls.json").read_text()))


@pytest.fixture
def brief() -> Brief:
    return Brief.model_validate(fixture_json("briefs/brief_12_sources.json"))


# normalize -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://WWW.Example.COM/Menu/", "https://www.example.com/Menu"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/a#reviews", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&utm_Medium=y&id=3", "https://example.com/a?id=3"),
        ("https://example.com/a?fbclid=abc&b=2&a=1", "https://example.com/a?b=2&a=1"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://user:pw@example.com/a", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_normalize(url, expected):
    assert normalize(url) == expected


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x", "mailto:a@b.com", "not a url", "https://", "http://[::1"]
)
def test_normalize_rejects_non_web_urls(url):
    assert normalize(url) is None


def test_scheme_is_not_upgraded():
    guard = CitationGuard(["http://example.com/a"])
    assert not guard.verified("https://example.com/a")


# observing -------------------------------------------------------------------------------


def test_observed_variants_match(guard):
    assert guard.verified(BOSTON_MAG)
    assert guard.verified(BOSTON_MAG.rstrip("/") + "?utm_campaign=share#top")
    assert guard.verified("HTTPS://WWW.BOSTONMAGAZINE.COM/restaurants/best-ramen-in-boston")
    assert not guard.verified(MADE_UP)


def test_observed_list_keeps_originals_once():
    guard = CitationGuard(["https://a.example/x/", "https://A.example/x", "https://b.example/"])
    guard.observe(["https://b.example/?utm_source=z"])
    assert guard.observed == ["https://a.example/x/", "https://b.example/"]


def test_real_stream_search_and_fetch_urls_are_observed():
    guard = CitationGuard(observed_from_stream("research_search_fetch.jsonl"))
    assert guard.verified(BOSTON_MAG)
    assert guard.verified("https://totalramen.com/massachusetts/boston")  # search hit, not read


def test_real_stream_failed_fetch_is_not_observed():
    guard = CitationGuard(observed_from_stream("fetch_404.jsonl"))
    assert not guard.verified("https://www.bostonmagazine.com/this-page-does-not-exist-milo-404")
    assert guard.observed == []


def test_collector_urls_count(guard):
    places = fixture_json("places/text_search_ok.json")["places"]
    assert guard.verified(places[0]["googleMapsUri"])


# briefs ----------------------------------------------------------------------------------


def test_brief_keeps_11_of_12(guard, brief):
    check = guard.check_brief(brief)
    assert (check.total, check.kept) == (12, 11)
    assert [s.url for s in check.removed] == [MADE_UP]
    assert [s.id for s in check.brief.sources] == list(range(1, 12))
    assert MADE_UP not in check.brief.model_dump_json()


def test_removed_ids_disappear_and_the_rest_follow_renumbering(guard, brief):
    check = guard.check_brief(brief)
    tora, santouka, ghost = check.brief.competitors
    assert tora.source_ids == [10, 1]  # unchanged: ids 1-11 keep their numbers here
    assert santouka.source_ids == []  # only cited the made-up source
    assert ghost.source_ids == []  # 99 never existed
    ideas = check.brief.campaign_ideas
    assert ideas[0].source_ids == [10, 11]
    assert ideas[2].source_ids == [3]  # duplicates collapse
    assert ideas[4].source_ids == []


def test_renumbering_after_a_removed_source_in_the_middle():
    guard = CitationGuard(["https://a.example/1", "https://a.example/3"])
    brief = Brief.model_validate(
        {
            **fixture_json("briefs/brief_12_sources.json"),
            "sources": [
                {"id": 1, "title": "One", "url": "https://a.example/1"},
                {"id": 2, "title": "Two", "url": "https://fake.example/2"},
                {"id": 3, "title": "Three", "url": "https://a.example/3"},
            ],
        }
    )
    brief.competitors[0].source_ids = [3, 2, 1]
    check = guard.check_brief(brief)
    assert [(s.id, s.title) for s in check.brief.sources] == [(1, "One"), (2, "Three")]
    assert check.brief.competitors[0].source_ids == [2, 1]


def test_the_same_page_cited_twice_becomes_one_source():
    guard = CitationGuard(["https://a.example/page"])
    brief = Brief.model_validate(
        {
            **fixture_json("briefs/brief_12_sources.json"),
            "sources": [
                {"id": 1, "title": "Page", "url": "https://a.example/page"},
                {"id": 2, "title": "Page again", "url": "https://a.example/page/?utm_source=x"},
            ],
        }
    )
    brief.campaign_ideas[0].source_ids = [2, 1]
    check = guard.check_brief(brief)
    assert check.kept == 1 and check.removed == []
    assert check.brief.campaign_ideas[0].source_ids == [1]


def test_urls_inside_brief_text_are_checked_too(guard, brief):
    snapshot = guard.check_brief(brief).brief.market_snapshot
    assert BOSTON_MAG in snapshot
    assert "fake.example.com" not in snapshot
    assert f"otherwise ({UNVERIFIED})." in snapshot


def test_brief_with_no_sources():
    brief = Brief.model_validate({**fixture_json("briefs/brief_12_sources.json"), "sources": []})
    check = CitationGuard().check_brief(brief)
    assert (check.total, check.kept, check.removed) == (0, 0, [])
    assert all(c.source_ids == [] for c in check.brief.competitors)


# free text -------------------------------------------------------------------------------


def test_clean_text_markdown_links(guard):
    text = f"See [Boston Magazine]({BOSTON_MAG}) and [the Globe]({MADE_UP})."
    cleaned, removed = guard.clean_text(text)
    assert cleaned == f"See [Boston Magazine]({BOSTON_MAG}) and the Globe {UNVERIFIED}."
    assert removed == 1


def test_clean_text_bare_urls_keep_trailing_punctuation(guard):
    cleaned, removed = guard.clean_text(f"Read {MADE_UP}. Or {BOSTON_MAG}, it's good.")
    assert cleaned == f"Read {UNVERIFIED}. Or {BOSTON_MAG}, it's good."
    assert removed == 1


def test_clean_text_urls_in_parentheses_and_angle_brackets(guard):
    cleaned, removed = guard.clean_text(f"(source: {MADE_UP}) <https://nope.example/x>")
    assert cleaned == f"(source: {UNVERIFIED}) <{UNVERIFIED}>"
    assert removed == 2


def test_clean_text_tracking_variant_of_an_observed_url_survives(guard):
    url = BOSTON_MAG + "?utm_source=newsletter"
    assert guard.clean_text(f"Go to {url}")[0] == f"Go to {url}"


def test_clean_text_without_urls_is_untouched(guard):
    assert guard.clean_text("No links here, just 5.0 stars.") == (
        "No links here, just 5.0 stars.",
        0,
    )


# grounding competitor numbers ------------------------------------------------------------


def places_data():
    return [
        {"name": "Tora Ramen", "rating": 4.5, "user_rating_count": 1287, "price_level": "$$"},
        {
            "name": "Hokkaido Ramen Santouka",
            "rating": 4.4,
            "user_rating_count": 3321,
            "price_level": "$$",
        },
        {"name": "Ramen Isshin", "rating": 4.0, "user_rating_count": 10, "price_level": "$"},
        {"name": "Isshin Ramen Bar", "rating": 4.1, "user_rating_count": 11, "price_level": "$"},
    ]


def test_numbers_come_from_places_not_the_model(brief):
    tora, santouka, ghost = ground_competitors(brief, places_data()).competitors
    assert (tora.rating, tora.review_count, tora.price_level) == (4.5, 1287, "$$")
    assert (santouka.rating, santouka.review_count) == (4.4, 3321)  # partial name match
    assert (ghost.rating, ghost.review_count, ghost.price_level) == (None, None, None)
    assert tora.positioning == "Chinatown favorite for rich tonkotsu"  # words stay the model's


def test_ambiguous_partial_names_get_no_numbers(brief):
    brief.competitors[0].name = "Isshin"
    assert ground_competitors(brief, places_data()).competitors[0].rating is None


def test_without_places_every_number_is_null(brief):
    for competitor in ground_competitors(brief, None).competitors:
        assert (competitor.rating, competitor.review_count, competitor.price_level) == (None,) * 3
