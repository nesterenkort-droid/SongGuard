"""Scanner types.

A scanner turns a platform query (an artist page, a search) into `RawCandidate`s —
the cheap, cross-platform facts we can capture before scoring. The detection service
(services/detection.py) normalizes, upserts them as global candidates, and scores
them against our tracks.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RawCandidate:
    platform: str
    native_id: str
    title: str
    url: str | None = None
    uploader: str | None = None  # crediting artist / channel
    description_raw: str | None = None
    parsed_provider: str | None = None  # e.g. "DistroKid"
    parsed_plabel: str | None = None  # e.g. "℗ 2026 13207436 Records DK"
    isrc: str | None = None
    published_at: date | None = None
    duration_ms: int | None = None
    thumb_url: str | None = None
    cover_url: str | None = None
    raw_json: dict = field(default_factory=dict)
