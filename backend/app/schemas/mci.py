from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Triage = Literal["red", "amber", "green", "black", "unassigned"]


class Patient(BaseModel):
    id: str
    triage: Triage = "unassigned"
    confirmed: bool = False  # a human has set or confirmed this colour
    suggested: Triage | None = None  # extracted suggestion, never used until confirmed
    suggested_evidence: str | None = None
    set_by: str | None = None
    note: str | None = None


class Load(BaseModel):
    id: str
    patient_ids: list[str]
    triage_mix: dict[str, int]
    hospital_id: str
    vehicle_needs: list[str]
    vehicle_id: str | None = None
    eta_scene_s: float | None = None
    eta_hospital_s: float | None = None
    assignment_id: str | None = None
    status: str = "planned"


class HospitalLoad(BaseModel):
    hospital_id: str
    name: str
    capacity: int  # patients it can take now (fresh, available data only)
    red_capacity: int
    planned: int
    planned_red: int
    en_route: int = 0
    fraction: float
    unverified_resources: list[str] = []
    note: str | None = None


class McPlan(BaseModel):
    created_at: datetime
    patient_hospital: dict[str, str]
    overflow: list[str]
    loads: list[Load]
    hospital_loads: list[HospitalLoad]
    unconfirmed: list[str]
    warnings: list[str]
    split: dict  # optimiser vs greedy
    assignment: dict  # vehicle assignment: ortools vs greedy
    rules: list[str]


class Mci(BaseModel):
    id: str
    incident_id: str
    created_at: datetime
    created_by: str
    patients: list[Patient]
    plan: McPlan | None = None
    status: Literal["triage", "planned", "approved"] = "triage"
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_reason: str | None = None


class MciCreate(BaseModel):
    incident_id: str
    patients: int = Field(ge=2, le=200)


class TriageSet(BaseModel):
    triage: Triage
    note: str | None = Field(default=None, max_length=200)


class SuggestBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ApproveBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
