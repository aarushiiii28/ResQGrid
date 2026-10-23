from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Source(StrEnum):
    reported = "reported"
    measured = "measured"
    ai_extracted = "ai_extracted"
    human_confirmed = "human_confirmed"
    simulated = "simulated"


# Higher wins when two sources disagree on a field.
SOURCE_RANK: dict[Source, int] = {
    Source.simulated: 0,
    Source.ai_extracted: 1,
    Source.reported: 2,
    Source.measured: 3,
    Source.human_confirmed: 4,
}


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    at: datetime | None = None
    approximate: bool = False
    source: Source = Source.reported


class Prov(BaseModel):
    """A field value with provenance."""

    value: Any
    source: Source
    at: datetime
    confidence: float = Field(default=1.0, ge=0, le=1)
    human_confirmed: bool = False
    evidence: str | None = None
