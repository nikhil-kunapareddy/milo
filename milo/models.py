"""Data shared across Milo: intake, collector results, the brief, and the session."""

from __future__ import annotations

import json
import re
from datetime import datetime
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


class Source(BaseModel):
    id: int
    title: str
    url: str


class Competitor(BaseModel):
    name: str
    rating: float | None
    review_count: int | None
    price_level: str | None
    positioning: str  # one line
    source_ids: list[int]


class CampaignIdea(BaseModel):
    title: str
    idea: str
    why_it_fits: str
    source_ids: list[int]


class CalendarDay(BaseModel):
    day: str  # "Mon"
    platform: str
    post: str


class Brief(BaseModel):
    market_snapshot: str
    competitors: list[Competitor]
    review_themes: list[str]
    content_benchmarks: list[str]  # what local content performs, if data exists
    gaps: list[str]
    campaign_ideas: list[CampaignIdea] = Field(min_length=5, max_length=5)
    content_calendar: list[CalendarDay] = Field(min_length=7, max_length=7)
    sources: list[Source]


class SessionState(StrEnum):
    INTAKE = "intake"
    AUDIENCE = "audience"
    COLLECTING = "collecting"
    RESEARCHING = "researching"
    FOLLOW_UP = "follow_up"
    ENDED = "ended"


class Turn(BaseModel):
    question: str
    answer: str  # after the citation guard
    asked_at: datetime


class Session(BaseModel):
    """Everything needed to resume or report on a session. Never holds API keys."""

    id: str  # "ramen-boston-7f3a"
    created: datetime
    updated: datetime
    state: SessionState  # the last active state; ENDED is never stored
    intake: Intake
    backend: str
    backend_session_id: str | None = None
    collectors: list[CollectorResult] = Field(default_factory=list)
    brief: Brief | None = None
    brief_raw: str | None = None  # the model's text when it never produced valid JSON
    turns: list[Turn] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)  # verified only
    observed_urls: list[str] = Field(default_factory=list)  # citation guard memory


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Read a JSON object from model text that may wrap it in code fences or prose."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    for candidate in (cleaned, cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None
