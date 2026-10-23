"""M18 false-alarm schemas. Features arrive as derived numbers only: no raw audio, no raw sensor streams."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Lane = Literal["HIGH", "UNCERTAIN", "LOW"]
FeatureStatus = Literal["available", "unavailable", "denied"]
LANE_RANK = {"LOW": 0, "UNCERTAIN": 1, "HIGH": 2}

# the only feature names the server accepts. Everything is a number in [0, 1] (speed_kmh excepted).
DEVICE_FEATURES = (
    "crash_like", "scream", "impact_transient", "mic_silence", "mic_loudness",
    "motion_erratic", "pocket_motion", "device_idle",
    "speed_kmh", "touch_deliberate", "touch_random",
)


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | None = Field(default=None, ge=0, le=400)
    status: FeatureStatus = "available"
    confidence: float | None = Field(default=None, ge=0, le=1)
    at: datetime | None = None


class SignalBatch(BaseModel):
    """What the phone sends. Derived on the device; the microphone only after explicit consent."""

    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(default=None, max_length=80)
    mic_consent: bool = False
    features: list[Feature] = Field(max_length=40)


class Contribution(BaseModel):
    feature: str
    evidence: float            # [-1, +1], 0 when unknown
    weight: float
    contribution: float        # weight * evidence, in logit units
    status: str                # available | unavailable | denied | derived | not_asked
    note: str | None = None


class Assessment(BaseModel):
    at: datetime
    probability_genuine: float
    lane: Lane
    base_lane: Lane            # lane from the score alone, before hard overrides
    logit: float
    bias: float
    contributions: list[Contribution]
    missing: list[str]         # signals that were unavailable or denied: treated as unknown
    overrides: list[str]       # hard overrides that raised the lane
    model_version: str
    config_hash: str
    trigger: str = "initial"


class Prompt(BaseModel):
    n: int
    at: datetime
    volume: float


class CheckIn(BaseModel):
    state: Literal["none", "prompting", "safe", "help", "danger", "unanswered"] = "none"
    started_at: datetime | None = None
    prompts: list[Prompt] = []
    response: str | None = None
    responded_at: datetime | None = None
    reason: str = "live"       # live | cancelled


class OutcomeTag(BaseModel):
    outcome: Literal["accidental", "genuine", "unknown"]
    reason: str
    by: str
    at: datetime


class FalseAlarmRecord(BaseModel):
    id: str                    # incident id
    created_at: datetime
    device_hash: str | None = None
    mic_consent: bool = False
    features: dict[str, Feature] = {}
    explicit_help: bool = False
    place_type: str | None = None
    corroborating: list[str] = []
    live_at: datetime | None = None
    cancelled_at: datetime | None = None
    hold_until: datetime | None = None     # LOW lane: dispatch waits until here, never longer
    dispatch_started: bool = False
    dispatcher_override: bool = False      # a dispatcher chose to dispatch: the caller's "I am safe" no longer stops it
    recalled_at: datetime | None = None    # an assignment was recalled because the caller said they are safe
    lane: Lane = "UNCERTAIN"
    min_lane: Lane | None = None           # floor: a timed-out hold or a dispatcher override can only raise the lane
    assessments: list[Assessment] = []
    checkin: CheckIn = CheckIn()
    tag: OutcomeTag | None = None
    close_reason: str | None = None
    followup_alert: bool = False


class SosStart(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    location_age_s: float = Field(default=0, ge=0)
    approximate: bool = False
    location_source: Literal["gps", "last_known", "manual_pin", "default"] = "gps"
    device_id: str | None = Field(default=None, max_length=80)


class CheckInResponse(BaseModel):
    response: Literal["safe", "help", "danger"]


class TagRequest(BaseModel):
    outcome: Literal["accidental", "genuine", "unknown"]
    reason: str = Field(min_length=3, max_length=300)


class CloseRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ModelProposal(BaseModel):
    version: str
    weights: dict[str, float]
    bias: float
    note: str = ""
    metrics: dict = {}
