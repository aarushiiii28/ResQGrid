from datetime import datetime, timedelta

from app.core.capabilities import vehicle_satisfies
from app.core.config import Settings
from app.db.repos import Repo
from app.schemas.common import GeoPoint, Source
from app.schemas.vehicle import (
    TRANSITIONS,
    HistoryEntry,
    UpdateResult,
    Vehicle,
    VehicleStateUpdate,
    VehicleView,
)
from app.services.audit_service import AuditService
from app.websockets.manager import EventHub

HISTORY_CAP = 50


class InvalidTransition(Exception):
    pass


class InvalidUpdate(Exception):
    pass


def make_view(v: Vehicle, now: datetime, stale_s: float) -> VehicleView:
    loc_age = (now - v.location_at).total_seconds() if v.location_at else None
    upd_age = (now - v.last_update_at).total_seconds() if v.last_update_at else None
    fresh = upd_age is not None and upd_age <= stale_s and not v.offline
    if v.location is None or loc_age is None:
        loc_state = "unknown"
    else:
        loc_state = "live" if loc_age <= stale_s and not v.offline else "stale"
    if v.status == "available":
        availability = "available" if fresh else "unknown"
    else:
        availability = "not_available"
    return VehicleView(
        id=v.id, capability_tags=v.capability_tags, status=v.status, availability=availability,
        location=v.location, location_state=loc_state,
        location_age_s=None if loc_age is None else round(loc_age, 1),
        last_update_age_s=None if upd_age is None else round(upd_age, 1),
        assigned_incident_id=v.assigned_incident_id,
        availability_estimate=v.availability_estimate, offline=v.offline, history=v.history[-10:],
    )


def eligibility(v: Vehicle, required: set[str], now: datetime, stale_s: float) -> tuple[bool, list[str]]:
    """Hard constraints. Returns (eligible, reason codes). Unknown is not available."""
    view = make_view(v, now, stale_s)
    reasons: list[str] = []
    if v.offline:
        reasons.append("offline")
    if v.status != "available":
        reasons.append(f"status_{v.status}")
    elif view.availability == "unknown" and not v.offline:
        reasons.append("availability_unknown_stale")
    if v.location is None:
        reasons.append("no_location")
    missing = sorted(t for t in required if not vehicle_satisfies(v.capability_tags, {t}))
    reasons += [f"missing_capability:{t}" for t in missing]
    return (not reasons), reasons


class VehicleService:
    def __init__(self, repo: Repo[Vehicle], incidents, audit: AuditService, hub: EventHub,
                 settings: Settings, now):
        self.repo, self.incidents, self.audit, self.hub = repo, incidents, audit, hub
        self.settings, self.now = settings, now

    def view(self, v: Vehicle) -> VehicleView:
        return make_view(v, self.now(), self.settings.vehicle_stale_s)

    async def list_views(self) -> list[VehicleView]:
        return [self.view(v) for v in await self.repo.list()]

    async def eligible(self, required: set[str]) -> list[tuple[Vehicle, bool, list[str]]]:
        now = self.now()
        out = []
        for v in await self.repo.list():
            ok, why = eligibility(v, required, now, self.settings.vehicle_stale_s)
            out.append((v, ok, why))
        return out

    async def apply_update(self, vehicle_id: str, upd: VehicleStateUpdate, actor: str) -> UpdateResult:
        now = self.now()
        if upd.occurred_at > now + timedelta(seconds=60):
            raise InvalidUpdate("occurred_at is in the future")
        if upd.status == "assigned" and not upd.incident_id:
            raise InvalidUpdate("incident_id required when status is 'assigned'")
        flags = {"applied": False, "ignored": False}
        status_change: dict = {}

        def fn(v: Vehicle) -> Vehicle | None:
            if upd.status is not None:
                if v.state_at and upd.occurred_at < v.state_at:
                    flags["ignored"] = True
                elif upd.status != v.status:
                    if upd.status not in TRANSITIONS[v.status]:
                        raise InvalidTransition(f"{v.status} -> {upd.status} is not allowed")
                    status_change.update(old=v.status, new=upd.status, inc=upd.incident_id or v.assigned_incident_id)
                    v.status = upd.status
                    v.state_at = upd.occurred_at
                    if upd.status == "assigned":
                        v.assigned_incident_id = upd.incident_id
                    elif upd.status == "available":
                        v.assigned_incident_id = None
                    self._hist(v, "status", f"{status_change['old']} -> {upd.status}", actor, now)
                    flags["applied"] = True
            if upd.location is not None:
                if v.location_at and upd.occurred_at < v.location_at:
                    flags["ignored"] = True
                else:
                    loc: GeoPoint = upd.location.model_copy(
                        update={"at": upd.occurred_at, "source": Source(upd.source)}
                    )
                    v.location, v.location_at = loc, upd.occurred_at
                    flags["applied"] = True
            if upd.offline is not None and upd.offline != v.offline:
                v.offline = upd.offline
                self._hist(v, "status", "offline" if upd.offline else "reconnected", actor, now)
                flags["applied"] = True
            if not flags["applied"]:
                return None
            if v.last_update_at is None or upd.occurred_at > v.last_update_at:
                v.last_update_at = upd.occurred_at
            return v

        v, outcome = await self.repo.modify(vehicle_id, fn, claim_key=f"vehicle:{vehicle_id}:{upd.update_id}")
        if outcome == "duplicate":
            return UpdateResult(applied=False, reason="duplicate")
        if outcome == "noop":
            return UpdateResult(applied=False, reason="out_of_order" if flags["ignored"] else "noop")

        inc_id = v.assigned_incident_id or status_change.get("inc")
        vis = {"vehicle_id": v.id}
        if inc_id:
            inc = await self.incidents.get(inc_id)
            if inc:
                vis |= {"caller": inc.created_by, "hospital_id": inc.destination_id}
        if status_change:
            await self.audit.record(
                entity_type="vehicle", entity_id=v.id, event_type="vehicle.status_changed",
                actor=actor, correlation_id=inc_id, source=upd.source, reason=upd.reason,
                old=status_change["old"], new=status_change["new"], vis=vis,
            )
        if upd.location is not None and not flags["ignored"]:
            self.hub.publish(
                "vehicle.location_updated", v.id, correlation_id=inc_id, source=upd.source,
                payload={"lat": v.location.lat, "lon": v.location.lon,
                         "accuracy_m": v.location.accuracy_m,
                         "occurred_at": upd.occurred_at.isoformat()}, vis=vis,
            )
        if upd.offline is not None:
            await self.audit.record(
                entity_type="vehicle", entity_id=v.id,
                event_type="vehicle.offline" if v.offline else "vehicle.reconnected",
                actor=actor, correlation_id=inc_id, source=upd.source, vis=vis,
            )
        return UpdateResult(applied=True, reason="applied")

    @staticmethod
    def _hist(v: Vehicle, kind: str, detail: str, actor: str, now: datetime) -> None:
        v.history.append(HistoryEntry(at=now, kind=kind, detail=detail, actor=actor))  # type: ignore[arg-type]
        del v.history[:-HISTORY_CAP]
