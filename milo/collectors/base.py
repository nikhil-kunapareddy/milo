"""Shared collector pieces: the Collector protocol, the runner, and Google error handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx

from milo.models import CollectorResult, Intake

COLLECTOR_TIMEOUT = 20.0  # seconds per collector, all requests included
REQUEST_TIMEOUT = 10.0  # seconds per HTTP request


class Collector(Protocol):
    name: str  # "google_places"
    label: str  # "Google Places"

    def available(self) -> bool: ...

    async def collect(self, intake: Intake) -> CollectorResult: ...


async def collect_all(
    collectors: Sequence[Collector], intake: Intake, timeout: float = COLLECTOR_TIMEOUT
) -> list[CollectorResult]:
    """Run every collector at once. Always returns one result per collector, in order."""

    async def run(collector: Collector) -> CollectorResult:
        if not collector.available():
            return CollectorResult(source=collector.name, status="skipped", note="no key")
        try:
            return await asyncio.wait_for(collector.collect(intake), timeout)
        except TimeoutError:
            return failed(collector.name, f"timed out after {timeout:g}s")
        except Exception as exc:  # collect() shouldn't raise; this is the backstop
            return failed(collector.name, f"unexpected error ({type(exc).__name__})")

    return list(await asyncio.gather(*(run(c) for c in collectors)))


def failed(source: str, note: str) -> CollectorResult:
    return CollectorResult(source=source, status="error", note=note)


class CollectorError(Exception):
    """A request failed; `note` is safe to show the user."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


async def read_json(request: Awaitable[httpx.Response]) -> dict[str, Any]:
    """Await one Google API call. Raises CollectorError with a short note on any failure."""
    try:
        response = await request
    except httpx.HTTPError as exc:
        raise CollectorError(network_error(exc).note) from exc
    if not response.is_success:
        raise CollectorError(google_error(response).note)
    try:
        body = response.json()
    except ValueError as exc:
        raise CollectorError("unexpected response from Google") from exc
    if not isinstance(body, dict):
        raise CollectorError("unexpected response from Google")
    return body


class KeyStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"  # rejected, API disabled, billing off, or restricted
    QUOTA = "quota"  # key is fine but out of quota
    UNREACHABLE = "unreachable"  # network trouble or a Google outage: can't tell


@dataclass(frozen=True)
class KeyCheck:
    status: KeyStatus
    note: str  # short and user-facing, e.g. "invalid key"


_REASONS: dict[str, KeyCheck] = {
    "API_KEY_INVALID": KeyCheck(KeyStatus.INVALID, "invalid key"),
    "keyInvalid": KeyCheck(KeyStatus.INVALID, "invalid key"),
    "SERVICE_DISABLED": KeyCheck(KeyStatus.INVALID, "API not enabled for this key's project"),
    "accessNotConfigured": KeyCheck(KeyStatus.INVALID, "API not enabled for this key's project"),
    "API_KEY_SERVICE_BLOCKED": KeyCheck(KeyStatus.INVALID, "key isn't allowed to use this API"),
    "BILLING_DISABLED": KeyCheck(KeyStatus.INVALID, "billing not enabled for this key's project"),
    "RATE_LIMIT_EXCEEDED": KeyCheck(KeyStatus.QUOTA, "quota exceeded"),
    "quotaExceeded": KeyCheck(KeyStatus.QUOTA, "quota exceeded"),
    "dailyLimitExceeded": KeyCheck(KeyStatus.QUOTA, "quota exceeded"),
    "rateLimitExceeded": KeyCheck(KeyStatus.QUOTA, "quota exceeded"),
}


def google_error(response: httpx.Response) -> KeyCheck:
    """Turn a failed Google API response into a short, key-free explanation."""
    for reason in _error_reasons(response):
        if reason in _REASONS:
            return _REASONS[reason]
        if reason.startswith("API_KEY_") and reason.endswith("_BLOCKED"):
            return KeyCheck(KeyStatus.INVALID, "key restrictions block this request")
    code = response.status_code
    if code == 429:
        return KeyCheck(KeyStatus.QUOTA, "quota exceeded")
    if code >= 500:
        return KeyCheck(KeyStatus.UNREACHABLE, f"Google returned HTTP {code}")
    return KeyCheck(KeyStatus.INVALID, f"request rejected (HTTP {code})")


def network_error(exc: Exception) -> KeyCheck:
    """Describe a transport failure without echoing URLs or headers."""
    if isinstance(exc, httpx.TimeoutException):
        return KeyCheck(KeyStatus.UNREACHABLE, "timed out")
    return KeyCheck(KeyStatus.UNREACHABLE, "network error")


def _error_reasons(response: httpx.Response) -> list[str]:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return []
    if not isinstance(error, dict):
        return []
    reasons = [d.get("reason") for d in error.get("details", []) if isinstance(d, dict)]
    reasons += [e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)]
    return [r for r in reasons if isinstance(r, str)]
