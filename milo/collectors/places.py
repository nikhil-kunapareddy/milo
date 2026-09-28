"""Google Places API (New): competitors, ratings, and review text."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

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

MAX_PLACES = 8
MAX_REVIEWED = 5  # Place Details calls, one per place
MAX_REVIEW_CHARS = 1000

SEARCH_FIELDS = ",".join(
    f"places.{field}"
    for field in (
        "id",
        "displayName",
        "rating",
        "userRatingCount",
        "priceLevel",
        "priceRange",
        "formattedAddress",
        "websiteUri",
        "googleMapsUri",
    )
)
DETAILS_FIELDS = "reviews"  # Place Details field names take no "places." prefix

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
        key = self._key
        if not key:
            return CollectorResult(source=self.name, status="skipped", note="no key")
        query = f"{intake.what} in {intake.where}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                places = await _search(client, key, query)
                top = places[:MAX_REVIEWED]
                reviews = await asyncio.gather(*(_reviews(client, key, p["id"]) for p in top))
        except CollectorError as exc:
            return failed(self.name, exc.note)
        except Exception as exc:  # never raise out of a collector
            return failed(self.name, f"unexpected error ({type(exc).__name__})")

        missing = 0
        for place, place_reviews in zip(top, reviews, strict=True):
            if place_reviews is None:
                missing += 1
            else:
                place["reviews"] = place_reviews
        for place in places:
            del place["id"]

        notes = []
        if not places:
            notes.append("no matching places found")
        if missing:
            notes.append(f"reviews unavailable for {missing} of {len(top)} places")
        urls = [u for p in places for u in (p["website"], p["maps_url"]) if u]
        return CollectorResult(
            source=self.name,
            status="ok",
            data={"query": query, "places": places},
            note="; ".join(notes) or None,
            urls=urls,
        )


async def _search(client: httpx.AsyncClient, key: str, query: str) -> list[dict[str, Any]]:
    body = await read_json(
        client.post(
            TEXT_SEARCH_URL,
            headers=headers(key, SEARCH_FIELDS),
            json={"textQuery": query, "pageSize": MAX_PLACES},
        )
    )
    try:
        return [_place(raw) for raw in body.get("places", [])[:MAX_PLACES]]
    except (KeyError, TypeError, AttributeError) as exc:
        raise CollectorError("unexpected response from Google") from exc


async def _reviews(client: httpx.AsyncClient, key: str, place_id: str) -> list[dict] | None:
    """Up to 5 reviews (Google's limit), or None if this one lookup fails."""
    try:
        body = await read_json(
            client.get(
                f"{API_ROOT}/places/{quote(place_id, safe='')}",
                headers=headers(key, DETAILS_FIELDS),
            )
        )
        return [_review(raw) for raw in body.get("reviews", [])]
    except (CollectorError, KeyError, TypeError, AttributeError):
        return None


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
    return {
        "id": raw["id"],
        "name": (raw.get("displayName") or {}).get("text") or "Unnamed place",
        "rating": raw.get("rating"),
        "user_rating_count": raw.get("userRatingCount"),
        "price_level": PRICE_LEVELS.get(raw.get("priceLevel", "")),
        "price_range": _price_range(raw.get("priceRange")),
        "address": raw.get("formattedAddress"),
        "website": raw.get("websiteUri"),
        "maps_url": raw.get("googleMapsUri"),
    }


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
