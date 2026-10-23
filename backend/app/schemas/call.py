from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.capabilities import CATEGORY_VEHICLE_TAGS

CATEGORIES = tuple(c for c in CATEGORY_VEHICLE_TAGS if c != "unknown")


class SosRequest(BaseModel):
    """The only thing the caller sends: where they are. No forms."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    location_age_s: float = Field(default=0, ge=0)
    approximate: bool = False
    location_source: Literal["gps", "last_known", "manual_pin", "default"] = "gps"
    device_id: str | None = Field(default=None, max_length=80)  # random per-install id; stored only hashed


class LocationUpdate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    location_age_s: float = Field(default=0, ge=0)
    approximate: bool = False
    location_source: Literal["gps", "last_known", "manual_pin", "default"] = "gps"


class Utterance(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    mode: Literal["speech", "typed", "quick", "scripted"] = "typed"


class TranscriptLine(BaseModel):
    speaker: Literal["caller", "operator", "system"]
    text: str
    at: datetime
    mode: str = "typed"


class CallSession(BaseModel):
    id: str
    incident_id: str
    started_at: datetime
    ended_at: datetime | None = None
    transcript: list[TranscriptLine] = []
    questions_asked: int = 0
    asked_keys: list[str] = []
    mode: str = "in_app_simulated"  # never a real phone call; raw audio is never stored


# ---- M12 strict extraction schema ------------------------------------------------
class ExtractedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=300)


class ExtractionOutput(BaseModel):
    """Strict output schema. The extractor may only fill these fields; it cannot dispatch,
    diagnose, assign urgency or reassign."""

    model_config = ConfigDict(extra="forbid")

    reported_category: ExtractedValue | None = None
    conscious: ExtractedValue | None = None
    breathing: ExtractedValue | None = None
    patient_count: ExtractedValue | None = None

    @field_validator("reported_category")
    @classmethod
    def _cat(cls, v: ExtractedValue | None):
        if v is not None and v.value not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return v

    @field_validator("conscious", "breathing")
    @classmethod
    def _bool(cls, v: ExtractedValue | None):
        if v is not None and not isinstance(v.value, bool):
            raise ValueError("must be true or false")
        return v

    @field_validator("patient_count")
    @classmethod
    def _count(cls, v: ExtractedValue | None):
        if v is not None and (isinstance(v.value, bool) or not isinstance(v.value, int) or not 1 <= v.value <= 500):
            raise ValueError("patient_count must be an integer 1..500")
        return v


class ExtractionResult(BaseModel):
    adapter: Literal["rules", "llm"]
    output: ExtractionOutput
    valid: bool = True
    review_required: bool = False
    review_reasons: list[str] = []
    applied: list[str] = []


class UtteranceResponse(BaseModel):
    transcript: list[TranscriptLine]
    operator: str | None
    questions_asked: int
    extraction: ExtractionResult | None
    finished: bool
