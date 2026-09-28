"""YouTube Data API v3: local food videos and how they perform."""

from __future__ import annotations

import httpx

from milo.collectors.base import KeyCheck, KeyStatus, google_error, network_error

API_ROOT = "https://www.googleapis.com/youtube/v3"
KEY_CHECK_TIMEOUT = 10.0


def headers(key: str) -> dict[str, str]:
    # The header keeps the key out of URLs, so it can't leak through logged requests.
    return {"X-Goog-Api-Key": key}


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
