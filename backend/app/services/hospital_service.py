from datetime import datetime, timedelta

from app.core.capabilities import HOSPITAL_RESOURCES
from app.core.config import Settings
from app.db.repos import Repo
from app.schemas.common import Source
from app.schemas.hospital import (
    Hospital,
    HospitalView,
    Resource,
    ResourceUpdate,
    ResourceView,
)
from app.schemas.vehicle import UpdateResult
from app.services.audit_service import AuditService


class InvalidResourceUpdate(Exception):
    pass


def resource_view(r: Resource, now: datetime, stale_s: float) -> ResourceView:
    age = (now - r.last_confirmed_at).total_seconds() if r.last_confirmed_at else None
    stale = age is None or age > stale_s
    # Unknown is not available: a stale or never-confirmed "available" is shown as unknown.
    eff = r.status if (r.status != "available" or not stale) else "unknown"
    return ResourceView(
        status=eff, reported_status=r.status, count=r.count, stale=stale,
        age_s=None if age is None else round(age, 1),
        last_confirmed_at=r.last_confirmed_at, source=r.source,
    )


def make_view(h: Hospital, now: datetime, stale_s: float) -> HospitalView:
    return HospitalView(
        id=h.id, name=h.name, location=h.location, capabilities=h.capabilities,
        resources={k: resource_view(r, now, stale_s) for k, r in h.resources.items()},
        last_confirmed_at=h.last_confirmed_at, source=h.source,
    )


class HospitalService:
    def __init__(self, repo: Repo[Hospital], audit: AuditService, settings: Settings, now):
        self.repo, self.audit, self.settings, self.now = repo, audit, settings, now

    def view(self, h: Hospital) -> HospitalView:
        return make_view(h, self.now(), self.settings.hospital_stale_s)

    async def list_views(self) -> list[HospitalView]:
        return [self.view(h) for h in await self.repo.list()]

    async def apply_resource_update(self, hospital_id: str, upd: ResourceUpdate, actor: str) -> UpdateResult:
        now = self.now()
        if upd.resource not in HOSPITAL_RESOURCES:
            raise InvalidResourceUpdate(f"unknown resource '{upd.resource}'")
        if upd.occurred_at > now + timedelta(seconds=60):
            raise InvalidResourceUpdate("occurred_at is in the future")
        snap: dict = {}

        def fn(h: Hospital) -> Hospital | None:
            cur = h.resources.get(upd.resource) or Resource()
            if cur.last_confirmed_at and upd.occurred_at < cur.last_confirmed_at:
                snap["ignored"] = True
                return None
            snap["old"] = cur.model_dump(mode="json", include={"status", "count", "last_confirmed_at", "source"})
            h.resources[upd.resource] = Resource(
                status=upd.status, count=upd.count, last_confirmed_at=upd.occurred_at,
                source=Source(upd.source), last_update_id=upd.update_id,
            )
            h.last_confirmed_at = max(filter(None, [h.last_confirmed_at, upd.occurred_at]))
            return h

        h, outcome = await self.repo.modify(
            hospital_id, fn, claim_key=f"hospital:{hospital_id}:{upd.update_id}"
        )
        if outcome == "duplicate":
            return UpdateResult(applied=False, reason="duplicate")
        if outcome == "noop":
            return UpdateResult(applied=False, reason="out_of_order")
        await self.audit.record(
            entity_type="hospital", entity_id=hospital_id, event_type="hospital.resource_updated",
            actor=actor, source=upd.source, reason=upd.reason, old=snap["old"],
            new={"resource": upd.resource, "status": upd.status, "count": upd.count},
            payload={"resource": upd.resource}, vis={"hospital_id": hospital_id},
        )
        return UpdateResult(applied=True, reason="applied")
