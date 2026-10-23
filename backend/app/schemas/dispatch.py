from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.routing import EntranceDecision, RouteEstimate


class Weights(BaseModel):
    w_time: float = Field(ge=0)
    w_fresh: float = Field(ge=0)
    w_coverage: float = Field(ge=0)
    w_uncertainty: float = Field(ge=0)
    w_switch: float = Field(ge=0)
    transport_weight: float = Field(ge=0)


class WeightsOverride(BaseModel):
    w_time: float | None = Field(default=None, ge=0)
    w_fresh: float | None = Field(default=None, ge=0)
    w_coverage: float | None = Field(default=None, ge=0)
    w_uncertainty: float | None = Field(default=None, ge=0)
    w_switch: float | None = Field(default=None, ge=0)
    transport_weight: float | None = Field(default=None, ge=0)


class ScoreComponents(BaseModel):
    """Raw component values (minute-equivalents) and their weighted contributions."""

    time: float
    freshness: float
    coverage_loss: float
    uncertainty: float
    switch: float
    weighted: dict[str, float]
    total: float


class Candidate(BaseModel):
    vehicle_id: str
    hospital_id: str
    feasible: bool
    rank: int | None = None
    is_current: bool = False
    eta_scene_low_s: float | None = None
    eta_scene_s: float | None = None
    eta_scene_high_s: float | None = None
    eta_hospital_s: float | None = None
    eta_total_low_s: float | None = None
    eta_total_high_s: float | None = None
    components: ScoreComponents | None = None
    constraints: dict[str, bool] = {}
    reason_codes: list[str] = []


class Exclusion(BaseModel):
    kind: Literal["vehicle", "hospital"]
    id: str
    reasons: list[str]


class DispatchDecision(BaseModel):
    id: str
    incident_id: str
    policy: Literal["P0", "P1"]
    policy_version: str
    created_at: datetime
    status: Literal["pending", "approved", "rejected", "overridden", "superseded", "kept_current"] = "pending"
    weights: Weights
    candidates: list[Candidate]  # ranked feasible first, then infeasible
    exclusions: list[Exclusion]
    recommendation: Candidate | None
    baseline: Candidate | None  # P0 nearest-eligible choice, for comparison
    keep_current: bool = False
    assumptions: list[str]
    inputs: dict[str, Any]  # snapshot to reproduce the decision
    scene_route: RouteEstimate | None = None
    hospital_route: RouteEstimate | None = None
    entrance: EntranceDecision | None = None
    approved_by: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    final: dict[str, Any] | None = None
    assignment_id: str | None = None


class RecommendRequest(BaseModel):
    incident_id: str
    policy: Literal["P0", "P1"] = "P1"
    weights: WeightsOverride | None = None
    autopilot: bool = True  # schedule Demo Autopilot approval if autopilot is on


class ApproveRequest(BaseModel):
    action: Literal["approve", "reject", "override"] = "approve"
    reason: str = Field(min_length=3, max_length=500)
    vehicle_id: str | None = None  # override only
    hospital_id: str | None = None  # override only


class BatchRequest(BaseModel):
    incident_ids: list[str] = Field(min_length=1, max_length=20)


AckState = Literal[
    "created", "sent", "delivered_if_known", "acknowledged", "accepted", "rejected", "timed_out", "cancelled"
]


class AckEvent(BaseModel):
    state: AckState
    at: datetime
    actor: str
    note: str | None = None


class Assignment(BaseModel):
    id: str
    incident_id: str
    vehicle_id: str
    hospital_id: str | None = None
    decision_id: str | None = None
    state: AckState = "created"
    created_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    accepted_at: datetime | None = None
    rejected_at: datetime | None = None
    timed_out_at: datetime | None = None
    cancelled_at: datetime | None = None
    timeout_s: float
    late: bool = False  # acknowledged/accepted after a timeout
    history: list[AckEvent] = []
    fallback: dict[str, Any] | None = None
    channel: str = "simulated_app_push"


class AssignmentAction(BaseModel):
    note: str | None = Field(default=None, max_length=300)
