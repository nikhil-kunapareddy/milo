"""Shared collector pieces: key checks and Google API error handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx


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
