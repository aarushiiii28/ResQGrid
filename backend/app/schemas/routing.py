from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.access import Entrance


class RouteStep(BaseModel):
    instruction: str
    maneuver: str  # depart | turn-left | turn-right | continue | uturn | arrive ...
    distance_m: float
    duration_s: float
    lat: float
    lon: float
    name: str | None = None


class RouteEstimate(BaseModel):
    distance_m: float
    duration_low_s: float
    duration_s: float  # mid estimate
    duration_high_s: float
    polyline: list[tuple[float, float]]
    steps: list[RouteStep]
    source: Literal["osrm", "mock", "mock_fallback"]
    confidence: float = Field(ge=0, le=1)
    degraded: bool = False
    computed_at: datetime
    applied_disruptions: list[str] = []
    notes: list[str] = []
    feasible: bool = True


class MatchResult(BaseModel):
    """Map-matched GPS trace: fixes snapped onto the road network (or onto known routes for the mock matcher)."""

    points: list[tuple[float, float]]
    source: Literal["osrm", "mock", "mock_fallback"]
    confidence: float = Field(ge=0, le=1)
    degraded: bool = False
    snapped: int = 0


class Disruption(BaseModel):
    id: str
    kind: Literal["closure", "congestion", "event_zone", "blocked_entrance"]
    lat: float | None = None
    lon: float | None = None
    radius_m: float | None = None
    multiplier: float = Field(default=1.0, ge=1.0, le=10.0)
    entrance_id: str | None = None
    note: str | None = None
    created_by: str = "system"
    created_at: datetime
    expires_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at


class DisruptionCreate(BaseModel):
    kind: Literal["closure", "congestion", "event_zone", "blocked_entrance"]
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_m: float | None = Field(default=None, gt=0, le=20000)
    multiplier: float = Field(default=1.0, ge=1.0, le=10.0)
    entrance_id: str | None = None
    note: str | None = None
    ttl_s: float | None = Field(default=None, gt=0)


class RoutePreviewRequest(BaseModel):
    origin_lat: float = Field(ge=-90, le=90)
    origin_lon: float = Field(ge=-180, le=180)
    dest_lat: float | None = Field(default=None, ge=-90, le=90)
    dest_lon: float | None = Field(default=None, ge=-180, le=180)
    building_id: str | None = None  # route to the chosen entrance instead of a raw point


class EntranceCandidate(BaseModel):
    entrance: Entrance
    score: float
    usable: bool
    reasons: list[str]
    active_report_ids: list[str] = []


class EntranceDecision(BaseModel):
    building_id: str
    chosen: EntranceCandidate | None
    alternatives: list[EntranceCandidate]
    needs_confirmation: bool
    confirmation_request: str | None = None
    expired_report_ids: list[str] = []


class AccessReportCreate(BaseModel):
    entrance_id: str
    kind: Literal["blocked", "open", "gate_locked", "gate_open"]
    note: str | None = Field(default=None, max_length=300)
