"""Data shared across Milo: intake, collector results, and (later) the brief and session."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Audience(StrEnum):
    NEW_OPENING = "new_opening"
    EXISTING = "existing_restaurant"
    CREATOR = "creator"
    CHAIN = "chain_new_location"

    @property
    def label(self) -> str:
        return AUDIENCE_LABELS[self]


AUDIENCE_LABELS = {
    Audience.NEW_OPENING: "New opening",
    Audience.EXISTING: "Existing restaurant",
    Audience.CREATOR: "Creator",
    Audience.CHAIN: "Chain adding a location",
}


class Intake(BaseModel):
    what: str  # food, cuisine, or dish: "ramen"
    where: str  # city or neighborhood: "Fenway, Boston"
    audience: Audience | None = None
    request: str = ""  # the user's own words, which often carry the goal ("want more students")


class CollectorResult(BaseModel):
    source: str  # "google_places", "youtube"
    status: Literal["ok", "skipped", "error"]
    data: dict[str, Any] | None = None
    note: str | None = None  # "no key", "quota exceeded", ...
    urls: list[str] = Field(default_factory=list)  # URLs this collector legitimately surfaced
