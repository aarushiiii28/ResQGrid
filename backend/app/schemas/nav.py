from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.routing import EntranceDecision, RouteEstimate, RouteStep


class NavTarget(BaseModel):
    kind: Literal["scene", "hospital"]
    name: str
    lat: float
    lon: float


class RerouteProposal(BaseModel):
    id: str
    reason: Literal["closure", "eta_drift"]
    message: str
    old_remaining_s: float
    new_remaining_s: float
    delta_s: float
    route: RouteEstimate
    status: Literal["pending", "accepted", "declined"] = "pending"


class Backup(BaseModel):
    """A pre-checked alternative kept in reserve. Shown dimmed, and only when relevant."""

    id: str
    polyline: list[tuple[float, float]]
    duration_s: float
    feasible: bool
    label: str = "backup route"


class RouteNotice(BaseModel):
    """A same-destination route change the stable-route policy applied by itself. The driver acknowledges (one tap);
    nothing here asks for approval. The dispatcher is told with the reason and the minutes saved."""

    id: str
    kind: Literal["reroute", "rejoin"]
    reason: Literal["blocked", "wrong_turn", "congestion"]
    message: str
    saved_s: float  # positive = faster than staying; negative = slower but necessary (blocked road, wrong turn)
    old_remaining_s: float
    new_remaining_s: float
    reason_codes: list[str] = []
    at: datetime
    acknowledged: bool = False


class OffRoute(BaseModel):
    state: Literal["uncertain", "suspect", "off_route"]
    deviation_m: float
    message: str
    rejoin_in_m: float | None = None
    via: str | None = None


class RouteLock(BaseModel):
    locked: bool = True
    version: int = 1
    reroutes: int = 0
    policy_version: str
    committed_at: datetime


class Signal(BaseModel):
    id: str
    lat: float
    lon: float
    along_m: float


class NavPlan(BaseModel):
    vehicle_id: str
    incident_id: str
    phase: Literal["scene", "hospital"]
    vehicle_status: str
    target: NavTarget
    route: RouteEstimate
    along_m: float
    remaining_m: float
    remaining_low_s: float
    remaining_s: float
    remaining_high_s: float
    next_step: RouteStep | None
    distance_to_next_m: float | None
    last_100m: bool
    entrance: EntranceDecision | None = None
    reroute: RerouteProposal | None = None
    green_corridor: dict
    simulated_movement: bool = False
    route_lock: RouteLock | None = None
    route_notice: RouteNotice | None = None
    off_route: OffRoute | None = None
    backups: list[Backup] = []
