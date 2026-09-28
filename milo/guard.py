"""Citation guard: a URL reaches the user only if this session actually observed it.

Observed means it came back in a web search result, a successful page fetch, or a
collector result. Everything else is removed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from milo.models import Brief, Source

UNVERIFIED = "[unverified link removed]"
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid",
    "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi",
}  # fmt: skip
DEFAULT_PORTS = {"http": 80, "https": 443}

# A markdown link, or a bare URL. Bare URLs stop at whitespace and common wrapping characters.
_LINK = re.compile(
    r"\[(?P<label>[^\]\n]*)\]\((?P<md>https?://[^)\s]+)\)|(?P<bare>https?://[^\s<>()\[\]\"'`]+)"
)
_TRAILING = ".,;:!?*_"


def normalize(url: str) -> str | None:
    """Canonical form for comparing URLs, or None if it isn't an http(s) URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if port and port != DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
        ]
    )
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, query, ""))


@dataclass(frozen=True)
class BriefCheck:
    brief: Brief
    total: int  # citations the model gave
    kept: int
    removed: list[Source]


class CitationGuard:
    def __init__(self, observed: Iterable[str] = ()) -> None:
        self._observed: dict[str, str] = {}
        self.observe(observed)

    def observe(self, urls: Iterable[str]) -> None:
        for url in urls:
            key = normalize(url)
            if key and key not in self._observed:
                self._observed[key] = url

    @property
    def observed(self) -> list[str]:
        """Original URLs, in first-seen order (for saving with the session)."""
        return list(self._observed.values())

    def verified(self, url: str) -> bool:
        key = normalize(url)
        return key is not None and key in self._observed

    def clean_text(self, text: str) -> tuple[str, int]:
        """Replace unverified URLs in free text. Returns the text and how many were removed."""
        removed = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal removed
            if match.group("md"):
                if self.verified(match.group("md")):
                    return match.group(0)
                removed += 1
                label = match.group("label").strip()
                return f"{label} {UNVERIFIED}" if label else UNVERIFIED
            url = match.group("bare")
            trail = ""
            while url and url[-1] in _TRAILING:
                url, trail = url[:-1], url[-1] + trail
            if self.verified(url):
                return match.group(0)
            removed += 1
            return UNVERIFIED + trail

        return _LINK.sub(replace, text), removed

    def verified_links(self, text: str) -> list[tuple[str, str]]:
        """(label, url) for each verified link in the text; label is "" for bare URLs."""
        return [(label, url) for label, url in find_links(text) if self.verified(url)]

    def check_brief(self, brief: Brief) -> BriefCheck:
        """Drop unverified sources, renumber the rest 1..n, and fix every reference to them."""
        renumber: dict[int, int] = {}
        kept: list[Source] = []
        removed: list[Source] = []
        by_url: dict[str, int] = {}
        for source in brief.sources:
            key = normalize(source.url)
            if key is None or key not in self._observed:
                removed.append(source)
                continue
            if key in by_url:  # the same page cited twice: point both ids at one source
                renumber[source.id] = by_url[key]
                continue
            new_id = len(kept) + 1
            renumber[source.id] = by_url[key] = new_id
            kept.append(source.model_copy(update={"id": new_id}))

        def remap(ids: list[int]) -> list[int]:
            return list(dict.fromkeys(renumber[i] for i in ids if i in renumber))

        data = brief.model_dump()
        data["sources"] = [s.model_dump() for s in kept]
        for item in (*data["competitors"], *data["campaign_ideas"]):
            item["source_ids"] = remap(item["source_ids"])
        data = self._clean_strings(data, skip=frozenset({"sources"}))
        return BriefCheck(
            brief=Brief.model_validate(data),
            total=len(brief.sources),
            kept=len(kept),
            removed=removed,
        )

    def _clean_strings(self, value: Any, skip: frozenset[str] = frozenset()) -> Any:
        """Run clean_text over every string in a dumped model, so no field smuggles a URL."""
        if isinstance(value, str):
            return self.clean_text(value)[0]
        if isinstance(value, list):
            return [self._clean_strings(v) for v in value]
        if isinstance(value, dict):
            return {k: v if k in skip else self._clean_strings(v) for k, v in value.items()}
        return value


def find_links(text: str) -> list[tuple[str, str]]:
    """(label, url) for every link in the text, markdown or bare, in order."""
    links = []
    for match in _LINK.finditer(text):
        if match.group("md"):
            links.append((match.group("label").strip(), match.group("md")))
        else:
            links.append(("", match.group("bare").rstrip(_TRAILING)))
    return links


def ground_competitors(brief: Brief, places: list[dict[str, Any]] | None) -> Brief:
    """Ratings, review counts, and price levels come only from Google Places data.

    A competitor that matches a Places result gets Google's numbers; any other competitor
    gets nulls, so the model can never present a made-up rating.
    """
    by_name = {_name_key(p.get("name", "")): p for p in places or []}
    competitors = []
    for competitor in brief.competitors:
        match = _find_place(_name_key(competitor.name), by_name)
        competitors.append(
            competitor.model_copy(
                update={
                    "rating": match.get("rating") if match else None,
                    "review_count": match.get("user_rating_count") if match else None,
                    "price_level": match.get("price_level") if match else None,
                }
            )
        )
    return brief.model_copy(update={"competitors": competitors})


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _find_place(key: str, by_name: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not key:
        return None
    if key in by_name:
        return by_name[key]
    # "Santouka" vs "Hokkaido Ramen Santouka": accept one containing the other, if unique.
    matches = [p for k, p in by_name.items() if k and (key in k or k in key)]
    return matches[0] if len(matches) == 1 else None
