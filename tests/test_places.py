import re

import httpx
import pytest

from milo.collectors import places
from milo.collectors.places import PlacesCollector
from milo.models import Intake
from tests.conftest import fixture_json

KEY = "AIzaSyPLACESPLACESPLACESPLACESPLACES9aZ"
INTAKE = Intake(what="ramen", where="Fenway, Boston")
DETAILS = re.compile(rf"{re.escape(places.API_ROOT)}/places/[^/:]+$")


@pytest.fixture
def search_ok(http):
    return http.post(places.TEXT_SEARCH_URL).respond(
        200, json=fixture_json("places/text_search_ok.json")
    )


@pytest.fixture
def details_ok(http):
    return http.get(DETAILS).respond(200, json=fixture_json("places/details_reviews.json"))


async def test_collects_competitors_with_reviews_for_the_top_five(search_ok, details_ok):
    result = await PlacesCollector(KEY).collect(INTAKE)

    assert result.status == "ok" and result.note is None
    data = result.data
    assert data["query"] == "ramen in Fenway, Boston"
    assert len(data["places"]) == 6
    first = data["places"][0]
    assert first["name"] == "Tora Ramen"
    assert (first["rating"], first["user_rating_count"]) == (4.5, 1287)
    assert (first["price_level"], first["price_range"]) == ("$$", "$10–20")
    assert first["reviews"][1]["text"].startswith("Good noodles but $19")
    assert "authorAttribution" not in str(first["reviews"])  # reviewer identities stay out
    assert [("reviews" in p) for p in data["places"]] == [True] * 5 + [False]
    assert details_ok.call_count == 5


async def test_sends_key_and_field_masks_as_headers(search_ok, details_ok):
    await PlacesCollector(KEY).collect(INTAKE)
    search = search_ok.calls.last.request
    assert search.headers["X-Goog-Api-Key"] == KEY
    assert "places.priceLevel" in search.headers["X-Goog-FieldMask"]
    assert KEY not in str(search.url)
    details = details_ok.calls.last.request
    assert details.headers["X-Goog-FieldMask"] == "reviews"


async def test_surfaces_websites_and_maps_urls(search_ok, details_ok):
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert "https://www.toraramen.example.com/" in result.urls
    assert "https://maps.google.com/?cid=1000000000000000000" in result.urls
    assert len(result.urls) == 11  # 6 maps links + 5 websites (Isshindo has none)


async def test_missing_key_is_skipped_without_a_request(http):
    collector = PlacesCollector(None)
    assert not collector.available()
    result = await collector.collect(INTAKE)
    assert (result.status, result.note) == ("skipped", "no key")
    assert not http.calls


async def test_invalid_key(http):
    body = fixture_json("places/error_invalid_key.json")
    http.post(places.TEXT_SEARCH_URL).respond(400, json=body)
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "invalid key")
    assert result.urls == []


async def test_quota_exceeded(http):
    http.post(places.TEXT_SEARCH_URL).respond(429, json=fixture_json("places/error_quota.json"))
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "quota exceeded")


async def test_api_not_enabled(http):
    body = fixture_json("places/error_service_disabled.json")
    http.post(places.TEXT_SEARCH_URL).respond(403, json=body)
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "API not enabled for this key's project")


async def test_timeout(http):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "timed out")


async def test_network_error(http):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ConnectError("offline"))
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "network error")


async def test_one_failed_review_lookup_keeps_everything_else(search_ok, http):
    def details(request: httpx.Request) -> httpx.Response:
        if "ChIJganko" in request.url.path:
            return httpx.Response(503)
        return httpx.Response(200, json=fixture_json("places/details_reviews.json"))

    http.get(DETAILS).mock(side_effect=details)
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert result.status == "ok"
    assert result.note == "reviews unavailable for 1 of 5 places"
    assert "reviews" not in result.data["places"][1]
    assert "reviews" in result.data["places"][0]


async def test_no_results(http):
    http.post(places.TEXT_SEARCH_URL).respond(200, json={})
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert result.status == "ok"
    assert result.data["places"] == []
    assert result.note == "no matching places found"


async def test_malformed_response(http):
    http.post(places.TEXT_SEARCH_URL).respond(200, json={"places": [{"no_id": True}]})
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "unexpected response from Google")


async def test_non_json_response(http):
    http.post(places.TEXT_SEARCH_URL).respond(200, text="<html>captive portal</html>")
    result = await PlacesCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "unexpected response from Google")


def test_price_range_formats():
    usd = {"currencyCode": "USD", "units": "10"}
    assert places._price_range({"startPrice": usd, "endPrice": {"units": "20"}}) == "$10–20"
    assert places._price_range({"startPrice": usd}) == "$10+"
    eur = {"currencyCode": "EUR", "units": "15"}
    assert places._price_range({"startPrice": eur, "endPrice": {"units": "25"}}) == "EUR 15–25"
    assert places._price_range(None) is None
