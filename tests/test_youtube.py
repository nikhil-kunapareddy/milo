import httpx
import pytest

from milo.collectors import youtube
from milo.collectors.youtube import YouTubeCollector
from milo.models import Intake
from tests.conftest import fixture_json

KEY = "AIzaSyYOUTUBEYOUTUBEYOUTUBEYOUTUBEYOU7bQ"
INTAKE = Intake(what="ramen", where="Boston")


@pytest.fixture
def search_ok(http):
    return http.get(youtube.SEARCH_URL).respond(200, json=fixture_json("youtube/search_ok.json"))


@pytest.fixture
def videos_ok(http):
    return http.get(youtube.VIDEOS_URL).respond(200, json=fixture_json("youtube/videos_ok.json"))


async def test_collects_videos_sorted_by_views(search_ok, videos_ok):
    result = await YouTubeCollector(KEY).collect(INTAKE)

    assert result.status == "ok"
    videos = result.data["videos"]
    assert [v["views"] for v in videos] == [184233, 48211, 9120, 1507]
    top = videos[0]
    assert top["title"] == "Best RAMEN in Boston?! Trying 5 spots"
    assert (top["channel"], top["published"]) == ("Boston Food Crawl", "2026-05-02")
    assert videos[2]["likes"] is None  # hidden like counts stay unknown, not zero
    assert result.urls == [v["url"] for v in videos]
    assert result.urls[0] == "https://www.youtube.com/watch?v=vidAAAAAAA1"


async def test_requests_use_header_key_and_one_batched_details_call(search_ok, videos_ok):
    await YouTubeCollector(KEY).collect(INTAKE)
    search = search_ok.calls.last.request
    assert search.headers["X-Goog-Api-Key"] == KEY
    assert KEY not in str(search.url)
    assert search.url.params["q"] == "ramen Boston"
    assert search.url.params["type"] == "video"
    assert videos_ok.call_count == 1
    ids = videos_ok.calls.last.request.url.params["id"]
    assert ids == "vidAAAAAAA1,vidBBBBBBB2,vidCCCCCCC3,vidDDDDDDD4"


async def test_repeat_collect_in_a_session_hits_the_cache(search_ok, videos_ok):
    collector = YouTubeCollector(KEY)
    first = await collector.collect(INTAKE)
    second = await collector.collect(INTAKE)
    assert first == second
    assert search_ok.call_count == 1


async def test_failures_are_not_cached(http, search_ok, videos_ok):
    collector = YouTubeCollector(KEY)
    search_ok.side_effect = [
        httpx.ConnectError("offline"),
        httpx.Response(200, json=fixture_json("youtube/search_ok.json")),
    ]
    assert (await collector.collect(INTAKE)).status == "error"
    assert (await collector.collect(INTAKE)).status == "ok"


async def test_missing_key_is_skipped_without_a_request(http):
    collector = YouTubeCollector("")
    assert not collector.available()
    result = await collector.collect(INTAKE)
    assert (result.status, result.note) == ("skipped", "no key")
    assert not http.calls


async def test_invalid_key(http):
    http.get(youtube.SEARCH_URL).respond(400, json=fixture_json("youtube/error_invalid_key.json"))
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "invalid key")


async def test_quota_exceeded(http):
    http.get(youtube.SEARCH_URL).respond(403, json=fixture_json("youtube/error_quota.json"))
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "quota exceeded")


async def test_quota_exceeded_on_details_call(search_ok, http):
    http.get(youtube.VIDEOS_URL).respond(403, json=fixture_json("youtube/error_quota.json"))
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "quota exceeded")


async def test_timeout(http):
    http.get(youtube.SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "timed out")


async def test_network_error(http):
    http.get(youtube.SEARCH_URL).mock(side_effect=httpx.ConnectError("offline"))
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "network error")


async def test_no_results_skips_the_details_call(http, videos_ok):
    http.get(youtube.SEARCH_URL).respond(200, json={"items": []})
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert result.status == "ok"
    assert result.note == "no matching videos found"
    assert videos_ok.call_count == 0


async def test_malformed_response(http):
    http.get(youtube.SEARCH_URL).respond(200, json={"items": [{"id": "not-a-dict"}]})
    result = await YouTubeCollector(KEY).collect(INTAKE)
    assert (result.status, result.note) == ("error", "unexpected response from YouTube")
