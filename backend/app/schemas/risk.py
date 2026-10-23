from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RiskType = Literal[
    "stale_vehicle_gps",
    "no_acknowledgment",
    "route_closure",
    "stale_hospital_resource",
    "capacity_change",
    "capability_change",
    "eta_drift",
    "state_conflict",
    "competing_incident",
]
OptionKind = Literal[
    "keep_current_plan", "request_confirmation", "reroute", "alternate_destination", "propose_eligible_unit", "escalate"
]


class Evidence(BaseModel):
    key: str
    value: Any
    at: datetime | None = None


class Effects(BaseModel):
    """What choosing this option does to time, suitability, coverage and other incidents."""

    time_delta_s: float | None = None  # negative = faster than the current plan
    suitability: Literal["unchanged", "improved", "reduced", "unverified"] = "unchanged"
    suitability_note: str = ""
    coverage_delta: float | None = None  # fraction of the demand grid; positive = more coverage kept
    other_incidents: list[dict[str, str]] = []
    uncertainty: str = ""


class RecoveryOption(BaseModel):
    id: str
    kind: OptionKind
    label: str
    description: str
    effects: Effects
    trade_offs: list[str] = []
    benefit: float | None = None  # score improvement over the current plan (None when not a plan change)
    changes_plan: bool = False
    necessary: bool = False  # the trigger makes the current plan unsafe, so the benefit threshold does not apply
    below_threshold: bool = False
    feasible: bool = True
    recommended: bool = False
    payload: dict[str, Any] = {}
    requires_approval: bool = True


class ConfirmationRequest(BaseModel):
    id: str
    target_kind: Literal["vehicle", "hospital"]
    target_id: str
    resource: str | None = None
    message: str
    requested_at: datetime
    requested_by: str


class Risk(BaseModel):
    id: str
    incident_id: str
    type: RiskType
    severity: Literal["low", "medium", "high", "critical"]
    entity: str
    dedupe_key: str
    evidence: list[Evidence]
    affected_assumption: str
    detected_at: datetime
    last_seen_at: datetime
    status: Literal["open", "awaiting", "resolved", "cleared"] = "open"
    options: list[RecoveryOption] = []
    decision_id: str | None = None  # dispatch decision the plan-changing options point at
    options_at: datetime | None = None
    confirmations: list[ConfirmationRequest] = []
    escalated: bool = False
    resolution: dict[str, Any] | None = None
    seen_count: int = 1


class RecoverRequest(BaseModel):
    option_kind: OptionKind
    reason: str = Field(min_length=3, max_length=500)
