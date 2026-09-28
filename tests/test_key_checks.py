import httpx

from milo.collectors import places, youtube
from milo.collectors.base import KeyStatus
from tests.conftest import fixture_json

KEY = "AIzaSyTESTKEYTESTKEYTESTKEYTESTKEY12345"
YOUTUBE_LANGUAGES = f"{youtube.API_ROOT}/i18nLanguages"


async def test_places_valid_key_uses_free_ids_only_mask(http):
    route = http.post(places.TEXT_SEARCH_URL).respond(200, json={"places": [{"id": "abc"}]})
    result = await places.check_key(KEY)
    assert result.status is KeyStatus.VALID
    sent = route.calls.last.request
    assert sent.headers["X-Goog-Api-Key"] == KEY
    assert sent.headers["X-Goog-FieldMask"] == "places.id"


async def test_places_invalid_key(http):
    http.post(places.TEXT_SEARCH_URL).respond(
        400, json=fixture_json("places/error_invalid_key.json")
    )
    result = await places.check_key(KEY)
    assert (result.status, result.note) == (KeyStatus.INVALID, "invalid key")


async def test_places_api_not_enabled(http):
    body = fixture_json("places/error_service_disabled.json")
    http.post(places.TEXT_SEARCH_URL).respond(403, json=body)
    result = await places.check_key(KEY)
    assert result.status is KeyStatus.INVALID
    assert "not enabled" in result.note


async def test_places_quota(http):
    http.post(places.TEXT_SEARCH_URL).respond(429, json=fixture_json("places/error_quota.json"))
    assert (await places.check_key(KEY)).status is KeyStatus.QUOTA


async def test_places_timeout(http):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    result = await places.check_key(KEY)
    assert (result.status, result.note) == (KeyStatus.UNREACHABLE, "timed out")


async def test_places_network_error(http):
    http.post(places.TEXT_SEARCH_URL).mock(side_effect=httpx.ConnectError("dns failure"))
    result = await places.check_key(KEY)
    assert (result.status, result.note) == (KeyStatus.UNREACHABLE, "network error")


async def test_places_server_error_is_not_blamed_on_the_key(http):
    http.post(places.TEXT_SEARCH_URL).respond(503, text="unavailable")
    assert (await places.check_key(KEY)).status is KeyStatus.UNREACHABLE


async def test_youtube_valid_key_goes_in_a_header_not_the_url(http):
    route = http.get(YOUTUBE_LANGUAGES).respond(200, json={"items": []})
    result = await youtube.check_key(KEY)
    assert result.status is KeyStatus.VALID
    sent = route.calls.last.request
    assert sent.headers["X-Goog-Api-Key"] == KEY
    assert KEY not in str(sent.url)


async def test_youtube_invalid_key(http):
    body = fixture_json("youtube/error_invalid_key.json")
    http.get(YOUTUBE_LANGUAGES).respond(400, json=body)
    result = await youtube.check_key(KEY)
    assert (result.status, result.note) == (KeyStatus.INVALID, "invalid key")


async def test_youtube_quota(http):
    http.get(YOUTUBE_LANGUAGES).respond(403, json=fixture_json("youtube/error_quota.json"))
    assert (await youtube.check_key(KEY)).status is KeyStatus.QUOTA


async def test_notes_never_contain_the_key(http):
    http.get(YOUTUBE_LANGUAGES).mock(side_effect=httpx.ConnectError(f"failed: ?key={KEY}"))
    result = await youtube.check_key(KEY)
    assert KEY not in result.note
