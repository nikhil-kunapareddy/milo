"""Optional data sources. Each one degrades to "skipped" instead of failing a session."""

from __future__ import annotations

from milo import config
from milo.collectors.base import Collector
from milo.collectors.places import PlacesCollector
from milo.collectors.youtube import YouTubeCollector
from milo.models import CollectorResult

LABELS = {
    PlacesCollector.name: PlacesCollector.label,
    YouTubeCollector.name: YouTubeCollector.label,
}


def build(cfg: config.Config) -> list[Collector]:
    return [PlacesCollector(cfg.key(config.PLACES)), YouTubeCollector(cfg.key(config.YOUTUBE))]


def summary(result: CollectorResult) -> list[str]:
    """What a successful result found, as short lines: "Found 7 competitors (Google Places)"."""
    data = result.data or {}
    if result.source == PlacesCollector.name:
        places = data.get("places", [])
        reviewed = sum(1 for p in places if p.get("reviews"))
        lines = [f"Found {len(places)} competitors (Google Places)"]
        if reviewed:
            lines.append(f"Pulled reviews for {reviewed} places")
        return lines
    if result.source == YouTubeCollector.name:
        return [f"Found {len(data.get('videos', []))} local videos (YouTube)"]
    return [f"{LABELS.get(result.source, result.source)} ✓"]
