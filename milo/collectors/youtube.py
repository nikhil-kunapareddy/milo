"""YouTube Data API v3: local food videos and how they perform."""

from __future__ import annotations

from typing import Any

import httpx

from milo.collectors.base import (
    REQUEST_TIMEOUT,
    CollectorError,
    KeyCheck,
    KeyStatus,
    failed,
    google_error,
    network_error,
    read_json,
)
from milo.models import CollectorResult, Intake

API_ROOT = "https://www.googleapis.com/youtube/v3"
SEARCH_URL = f"{API_ROOT}/search"
VIDEOS_URL = f"{API_ROOT}/videos"
KEY_CHECK_TIMEOUT = 10.0
MAX_VIDEOS = 25  # search.list costs the same for 5 or 50 results


def headers(key: str) -> dict[str, str]:
    # The header keeps the key out of URLs, so it can't leak through logged requests.
    return {"X-Goog-Api-Key": key}


class YouTubeCollector:
    name = "youtube"
    label = "YouTube"

    def __init__(self, key: str | None) -> None:
        self._key = key
        # search.list draws on a small daily bucket, so never repeat a query in a session.
        self._cache: dict[str, CollectorResult] = {}

    def available(self) -> bool:
        return bool(self._key)

    async def collect(self, intake: Intake) -> CollectorResult:
        key = self._key
        if not key:
            return CollectorResult(source=self.name, status="skipped", note="no key")
        query = f"{intake.what} {intake.where}"
        if query in self._cache:
            return self._cache[query]
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers(key)) as client:
                ids = await _search(client, query)
                videos = await _details(client, ids) if ids else []
        except CollectorError as exc:
            return failed(self.name, exc.note)
        except Exception as exc:  # never raise out of a collector
            return failed(self.name, f"unexpected error ({type(exc).__name__})")

        videos.sort(key=lambda v: v["views"] or 0, reverse=True)
        result = CollectorResult(
            source=self.name,
            status="ok",
            data={"query": query, "videos": videos},
            note=None if videos else "no matching videos found",
            urls=[v["url"] for v in videos],
        )
        self._cache[query] = result
        return result


async def _search(client: httpx.AsyncClient, query: str) -> list[str]:
    body = await read_json(
        client.get(
            SEARCH_URL,
            params={"part": "snippet", "q": query, "type": "video", "maxResults": MAX_VIDEOS},
        )
    )
    try:
        return [item["id"]["videoId"] for item in body.get("items", [])]
    except (KeyError, TypeError) as exc:
        raise CollectorError("unexpected response from YouTube") from exc


async def _details(client: httpx.AsyncClient, ids: list[str]) -> list[dict[str, Any]]:
    """View counts and publish dates (1 quota unit for all of them)."""
    body = await read_json(
        client.get(VIDEOS_URL, params={"part": "snippet,statistics", "id": ",".join(ids)})
    )
    try:
        return [_video(item) for item in body.get("items", [])]
    except (KeyError, TypeError, AttributeError) as exc:
        raise CollectorError("unexpected response from YouTube") from exc


def _video(item: dict[str, Any]) -> dict[str, Any]:
    snippet, stats = item.get("snippet", {}), item.get("statistics", {})
    return {
        "title": snippet.get("title", ""),
        "channel": snippet.get("channelTitle", ""),
        "published": (snippet.get("publishedAt") or "")[:10] or None,
        "views": _count(stats.get("viewCount")),
        "likes": _count(stats.get("likeCount")),  # hidden by some channels
        "comments": _count(stats.get("commentCount")),
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


def _count(value: object) -> int | None:
    """The API sends counts as strings, and omits them when a channel hides them."""
    if isinstance(value, int | str) and str(value).isdigit():
        return int(value)
    return None


async def check_key(key: str) -> KeyCheck:
    """One i18nLanguages.list call, the cheapest request (1 quota unit)."""
    try:
        async with httpx.AsyncClient(timeout=KEY_CHECK_TIMEOUT) as client:
            response = await client.get(
                f"{API_ROOT}/i18nLanguages", headers=headers(key), params={"part": "snippet"}
            )
    except httpx.HTTPError as exc:
        return network_error(exc)
    if response.is_success:
        return KeyCheck(KeyStatus.VALID, "works")
    return google_error(response)
