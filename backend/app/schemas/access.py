"""M2 data model: buildings with multiple entrances and temporary access reports."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import GeoPoint, Source


class TempAccessReport(BaseModel):
    id: str
    entrance_id: str
    kind: Literal["blocked", "open", "gate_locked", "gate_open"]
    reported_at: datetime
    expires_at: datetime
    source: Source = Source.reported
    note: str | None = None
    reconfirmed_at: datetime | None = None


class Entrance(BaseModel):
    id: str
    name: str
    kind: Literal["main", "emergency", "rear", "gate", "side"]
    location: GeoPoint
    accessible: bool = True
    gate_restricted: bool = False
    landmark: str | None = None
    source: Source = Source.simulated
    confidence: float = 0.8
    verified_at: datetime | None = None


class Building(BaseModel):
    id: str
    name: str
    centroid: GeoPoint
    entrances: list[Entrance]
    temp_reports: list[TempAccessReport] = []


class Aed(BaseModel):
    id: str
    label: str
    location: GeoPoint
    available_hours: str = "24h"


class Volunteer(BaseModel):
    id: str
    name: str
    location: GeoPoint
    opted_in: bool = True
    certified: bool = True
    available: bool = True
