from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import GeoPoint, Source

ResourceStatus = Literal["available", "unavailable", "unknown"]


class Resource(BaseModel):
    status: ResourceStatus = "unknown"
    count: int | None = Field(default=None, ge=0)
    last_confirmed_at: datetime | None = None
    source: Source = Source.simulated
    last_update_id: str | None = None


class Hospital(BaseModel):
    id: str
    name: str
    location: GeoPoint
    capabilities: list[str]  # stable
    resources: dict[str, Resource]  # current
    last_confirmed_at: datetime | None = None
    source: Source = Source.simulated


class ResourceView(BaseModel):
    status: ResourceStatus  # effective: stale "available" becomes "unknown"
    reported_status: ResourceStatus
    count: int | None
    stale: bool
    age_s: float | None
    last_confirmed_at: datetime | None
    source: Source


class HospitalView(BaseModel):
    id: str
    name: str
    location: GeoPoint
    capabilities: list[str]
    resources: dict[str, ResourceView]
    last_confirmed_at: datetime | None
    source: Source


class ResourceUpdate(BaseModel):
    update_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    resource: str
    status: ResourceStatus
    count: int | None = Field(default=None, ge=0)
    source: Literal["reported", "measured", "human_confirmed", "simulated"] = "human_confirmed"
    reason: str | None = None
