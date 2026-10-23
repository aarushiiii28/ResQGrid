from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Source


class Observation(BaseModel):
    key: str
    value: Any
    source: Source
    at: datetime
    confidence: float = 1.0


class ChecklistItem(BaseModel):
    key: str
    label: str
    status: Literal["open", "provided", "not_applicable"] = "open"
    note: str | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None


class PreAlert(BaseModel):
    """M9. Distinguishes caller reports from confirmed/measured observations."""

    id: str
    incident_id: str
    hospital_id: str
    vehicle_id: str
    created_at: datetime
    updated_at: datetime
    reported_reason: str
    reported_category: str | None = None
    confirmed_observations: list[Observation] = []
    reported_observations: list[Observation] = []
    unknowns: list[str] = []
    eta_low_s: float | None = None
    eta_high_s: float | None = None
    eta_updated_at: datetime | None = None
    status: Literal[
        "incoming", "acknowledged", "arrived", "handover_started", "handover_completed", "superseded"
    ] = "incoming"
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    arrived_at: datetime | None = None
    arrival_source: str | None = None
    handover_started_at: datetime | None = None
    handover_completed_at: datetime | None = None
    handover_duration_s: float | None = None  # arrival -> handover_completed (simulated)
    checklist: list[ChecklistItem] = []
    superseded_reason: str | None = None


class ChecklistUpdate(BaseModel):
    key: str
    status: Literal["open", "provided", "not_applicable"]
    note: str | None = Field(default=None, max_length=300)
