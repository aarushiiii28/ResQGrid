from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import GeoPoint

VehicleStatus = Literal[
    "available", "assigned", "accepted", "en_route", "on_scene", "transporting", "unavailable"
]

TRANSITIONS: dict[str, set[str]] = {
    "available": {"assigned", "unavailable"},
    "assigned": {"accepted", "available", "unavailable"},
    "accepted": {"en_route", "available", "unavailable"},
    "en_route": {"on_scene", "available", "unavailable"},
    "on_scene": {"transporting", "available", "unavailable"},
    "transporting": {"available", "unavailable"},
    "unavailable": {"available"},
}


class HistoryEntry(BaseModel):
    at: datetime
    kind: Literal["status", "location", "ignored"]
    detail: str
    actor: str


class Vehicle(BaseModel):
    id: str
    capability_tags: list[str]
    status: VehicleStatus = "available"
    location: GeoPoint | None = None
    location_at: datetime | None = None
    state_at: datetime | None = None
    last_update_at: datetime | None = None
    assigned_incident_id: str | None = None
    availability_estimate: datetime | None = None
    base: GeoPoint | None = None
    offline: bool = False
    history: list[HistoryEntry] = []


class VehicleView(BaseModel):
    """What clients see: freshness is explicit, stale is never presented as live."""

    id: str
    capability_tags: list[str]
    status: VehicleStatus
    availability: Literal["available", "not_available", "unknown"]
    location: GeoPoint | None
    location_state: Literal["live", "stale", "unknown"]
    location_age_s: float | None
    last_update_age_s: float | None
    assigned_incident_id: str | None
    availability_estimate: datetime | None
    offline: bool
    history: list[HistoryEntry]


class VehicleStateUpdate(BaseModel):
    update_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    status: VehicleStatus | None = None
    location: GeoPoint | None = None
    offline: bool | None = None
    incident_id: str | None = None
    reason: str | None = None
    source: Literal["measured", "simulated", "reported"] = "simulated"


class UpdateResult(BaseModel):
    applied: bool
    reason: Literal["applied", "duplicate", "out_of_order", "noop"]
