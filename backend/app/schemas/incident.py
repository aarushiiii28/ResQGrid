from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import GeoPoint, Prov, Source

DEFAULT_UNKNOWN_KEYS = ["reported_category", "conscious", "patient_count", "exact_location"]


class Report(BaseModel):
    """Raw report, stored separately from extracted facts."""

    id: str
    incident_id: str
    original_incident_id: str | None = None
    text: str
    channel: Literal["typed", "voice_transcript", "form", "sos"] = "typed"
    reporter: str | None = None
    source: Source = Source.reported
    received_at: datetime


class DuplicateSuggestion(BaseModel):
    incident_id: str
    score: float
    distance_m: float | None
    minutes_apart: float
    text_similarity: float
    reasons: list[str]
    status: Literal["suggested", "dismissed", "merged"] = "suggested"


class Conflict(BaseModel):
    field: str
    existing: Any
    incoming: Any
    existing_source: Source
    incoming_source: Source
    detected_at: datetime
    resolved: bool = False


class LocationCandidate(GeoPoint):
    label: str | None = None
    building_id: str | None = None
    entrance_id: str | None = None


class MergeRecord(BaseModel):
    fact_keys: list[str] = []
    report_ids: list[str] = []
    merged_at: datetime


class Incident(BaseModel):
    id: str
    created_at: datetime
    created_by: str | None = None
    demo_mode: bool = True
    # pending = SOS countdown running (not dispatched, shown muted); cancelled_by_caller is kept forever
    status: Literal["pending", "new", "dispatched", "active", "closed", "merged", "cancelled_by_caller"] = "new"
    reported_category: Prov | None = None
    operator_priority: Prov | None = None
    patient_count: Prov | None = None
    location_candidates: list[LocationCandidate] = []
    access_details: dict[str, Any] = {}
    building_id: str | None = None
    facts: dict[str, Prov] = {}
    unknowns: list[str] = list(DEFAULT_UNKNOWN_KEYS)
    conflicts: list[Conflict] = []
    duplicate_suggestions: list[DuplicateSuggestion] = []
    assigned_vehicle_id: str | None = None
    destination_id: str | None = None
    # merge / split bookkeeping (reversible)
    merged_into: str | None = None
    status_before_merge: str | None = None
    merged_from: list[str] = []
    merge_log: dict[str, MergeRecord] = {}
    split_from: str | None = None
    # hospital case tracking (filled in Phase 4)
    hospital_case: dict[str, Any] = {}
    needs_review: bool = False
    review_reasons: list[str] = []
    channel: str = "form"
    location_source: str | None = None
    updated_at: datetime | None = None


class IncidentCreate(BaseModel):
    reported_category: str | None = None
    operator_priority: int | None = Field(default=None, ge=1, le=4)
    patient_count: int | None = Field(default=None, ge=0, le=500)
    location: GeoPoint | None = None
    location_label: str | None = None
    building_id: str | None = None
    text: str | None = Field(default=None, max_length=2000)
    channel: Literal["typed", "voice_transcript", "form", "sos"] = "form"
    facts: dict[str, Any] = {}


class ReportCreate(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    channel: Literal["typed", "voice_transcript", "form", "sos"] = "typed"
    location: GeoPoint | None = None


class FactSet(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: Any
    source: Source = Source.reported
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str | None = None
    reason: str | None = None


class MergeRequest(BaseModel):
    target_id: str
    reason: str = Field(min_length=3)


class SplitRequest(BaseModel):
    report_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=3)
