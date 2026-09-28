"""Google Places API (New): competitors, ratings, and review text."""

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

API_ROOT = "https://places.googleapis.com/v1"
TEXT_SEARCH_URL = f"{API_ROOT}/places:searchText"
KEY_CHECK_TIMEOUT = 10.0

MAX_PLACES = 20  # the most one Text Search page returns; billing is per request
MAX_REVIEW_CHARS = 1000

# Asking for reviews here puts the whole call on the Enterprise + Atmosphere SKU, but it's
# still one billed request instead of a Place Details call per place.
SEARCH_FIELDS = ",".join(
    f"places.{field}"
    for field in (
        "displayName",
        "rating",
        "userRatingCount",
        "priceLevel",
        "priceRange",
        "formattedAddress",
        "websiteUri",
        "googleMapsUri",
        "reviews",
    )
)

PRICE_LEVELS = {
    "PRICE_LEVEL_FREE": "Free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def headers(key: str, field_mask: str) -> dict[str, str]:
    return {"X-Goog-Api-Key": key, "X-Goog-FieldMask": field_mask}


class PlacesCollector:
    name = "google_places"
    label = "Google Places"

    def __init__(self, key: str | None) -> None:
        self._key = key

    def available(self) -> bool:
        return bool(self._key)

    async def collect(self, intake: Intake) -> CollectorResult:
        """One Text Search: competitors with ratings, prices, links, and up to 5 reviews each."""
        key = self._key
        if not key:
            return CollectorResult(source=self.name, status="skipped", note="no key")
        query = f"{intake.what} in {intake.where}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                body = await read_json(
                    client.post(
                        TEXT_SEARCH_URL,
                        headers=headers(key, SEARCH_FIELDS),
                        json={"textQuery": query, "pageSize": MAX_PLACES},
                    )
                )
            places = [_place(raw) for raw in body.get("places", [])[:MAX_PLACES]]
        except CollectorError as exc:
            return failed(self.name, exc.note)
        except (KeyError, TypeError, AttributeError):
            return failed(self.name, "unexpected response from Google")
        except Exception as exc:  # never raise out of a collector
            return failed(self.name, f"unexpected error ({type(exc).__name__})")

        urls = [u for p in places for u in (p["website"], p["maps_url"]) if u]
        return CollectorResult(
            source=self.name,
            status="ok",
            data={"query": query, "places": places},
            note=None if places else "no matching places found",
            urls=urls,
        )


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


def _place(raw: dict[str, Any]) -> dict[str, Any]:
    place = {
        "name": raw["displayName"]["text"],
        "rating": raw.get("rating"),
        "user_rating_count": raw.get("userRatingCount"),
        "price_level": PRICE_LEVELS.get(raw.get("priceLevel", "")),
        "price_range": _price_range(raw.get("priceRange")),
        "address": raw.get("formattedAddress"),
        "website": raw.get("websiteUri"),
        "maps_url": raw.get("googleMapsUri"),
    }
    reviews = [_review(r) for r in raw.get("reviews", [])]
    if reviews:
        place["reviews"] = reviews
    return place


def _review(raw: dict[str, Any]) -> dict[str, Any]:
    text = ((raw.get("text") or raw.get("originalText") or {}).get("text") or "").strip()
    return {
        "rating": raw.get("rating"),
        "when": raw.get("relativePublishTimeDescription"),
        "text": text[:MAX_REVIEW_CHARS],
    }


def _price_range(raw: dict[str, Any] | None) -> str | None:
    """`{"startPrice": {"currencyCode": "USD", "units": "10"}, ...}` -> "$10–20"."""
    if not raw or "startPrice" not in raw:
        return None
    start, end = raw["startPrice"], raw.get("endPrice")
    currency = start.get("currencyCode", "")
    symbol = "$" if currency == "USD" else f"{currency} "
    low = start.get("units", "0")
    return f"{symbol}{low}–{end.get('units')}" if end else f"{symbol}{low}+"
