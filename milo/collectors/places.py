"""Google Places API (New): competitors, ratings, and review text."""

from __future__ import annotations

import httpx

from milo.collectors.base import KeyCheck, KeyStatus, google_error, network_error

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
KEY_CHECK_TIMEOUT = 10.0


def headers(key: str, field_mask: str) -> dict[str, str]:
    return {"X-Goog-Api-Key": key, "X-Goog-FieldMask": field_mask}


async def check_key(key: str) -> KeyCheck:
    """One Text Search asking only for place IDs, which Google bills as free."""
    try:
        async with httpx.AsyncClient(timeout=KEY_CHECK_TIMEOUT) as client:
            response = await client.post(
                TEXT_SEARCH_URL,
                headers=headers(key, "places.id"),
                json={"textQuery": "restaurant", "pageSize": 1},
            )
    except httpx.HTTPError as exc:
        return network_error(exc)
    if response.is_success:
        return KeyCheck(KeyStatus.VALID, "works")
    return google_error(response)
