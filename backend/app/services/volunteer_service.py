"""M14 nearest AED and simulated volunteer alerts. Volunteers are synthetic, opt-in, and nothing is
sent outside the app. Walking times are straight-line x 1.3 (street factor) / 1.4 m/s; a prototype
approximation, shown as a range."""

import asyncio

from pydantic import BaseModel

from app.core.geo import haversine_m
from app.db.repos import Repo
from app.schemas.incident import Incident
from app.services.incident_service import RuleViolation, vis_for

WALK_MPS = 1.4
STREET_FACTOR = 1.3
CATEGORIES_WITH_VOLUNTEERS = {"cardiac", "unconscious", "breathing"}


class AedNear(BaseModel):
    id: str
    label: str
    lat: float
    lon: float
    distance_m: float
    walk_low_s: float
    walk_s: float
    walk_high_s: float


class VolunteerAlert(BaseModel):
    id: str
    incident_id: str
    volunteer_id: str
    volunteer_name: str
    distance_m: float
    walk_s: float
    aed_id: str | None = None
    aed_label: str | None = None
    status: str = "sent"  # sent | accepted | declined | stood_down
    sent_at: str
    responded_at: str | None = None
    simulated: bool = True


def walk_time(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, float, float]:
    d = haversine_m(*a, *b) * STREET_FACTOR
    mid = d / WALK_MPS
    return d, mid * 0.8, mid, mid * 1.3


class VolunteerService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[VolunteerAlert] = Repo(ctx.store, "valert", VolunteerAlert)

    async def nearest_aeds(self, inc: Incident, n: int = 3) -> list[AedNear]:
        if not inc.location_candidates:
            return []
        loc = (inc.location_candidates[0].lat, inc.location_candidates[0].lon)
        out = []
        for a in await self.ctx.aed_repo.list():
            d, lo, mid, hi = walk_time(loc, (a.location.lat, a.location.lon))
            out.append(AedNear(id=a.id, label=a.label, lat=a.location.lat, lon=a.location.lon,
                               distance_m=round(d), walk_low_s=round(lo), walk_s=round(mid), walk_high_s=round(hi)))
        return sorted(out, key=lambda x: (x.walk_s, x.id))[:n]

    async def list_for(self, incident_id: str) -> list[VolunteerAlert]:
        return [a for a in await self.repo.list() if a.incident_id == incident_id]

    async def alert(self, inc: Incident, actor: str, max_n: int = 3) -> list[VolunteerAlert]:
        """Alert opted-in, available synthetic volunteers near the scene. Idempotent per incident."""
        ctx = self.ctx
        if await self.list_for(inc.id):
            return await self.list_for(inc.id)
        if not inc.location_candidates:
            raise RuleViolation("incident has no location")
        loc = inc.location_candidates[0]
        aed = (await self.nearest_aeds(inc, 1) or [None])[0]
        near = []
        for v in await ctx.volunteer_repo.list():
            if not (v.opted_in and v.available and v.certified):
                continue
            d = haversine_m(loc.lat, loc.lon, v.location.lat, v.location.lon)
            if d <= ctx.settings.volunteer_radius_m:
                near.append((d, v))
        near.sort(key=lambda t: (t[0], t[1].id))
        out = []
        for d, v in near[:max_n]:
            n = await ctx.store.next_seq("valert_seq")
            a = VolunteerAlert(
                id=f"VA-{n:04d}", incident_id=inc.id, volunteer_id=v.id, volunteer_name=v.name,
                distance_m=round(d * STREET_FACTOR), walk_s=round(d * STREET_FACTOR / WALK_MPS),
                aed_id=aed.id if aed else None, aed_label=aed.label if aed else None, sent_at=ctx.now().isoformat())
            await self.repo.put(a)
            await ctx.audit.record(
                entity_type="volunteer_alert", entity_id=a.id, event_type="volunteer.alert_sent", actor=actor,
                correlation_id=inc.id, source="simulated",
                payload={"volunteer_id": v.id, "distance_m": a.distance_m, "note": "simulated alert; no message sent"},
                vis=vis_for(inc))
            out.append(a)
        if out and ctx.settings.sim_volunteers:
            ctx.spawn(self._simulate_responses(inc.id, [a.id for a in out]))
        return out

    async def respond(self, alert_id: str, accept: bool, actor: str) -> VolunteerAlert:
        ctx = self.ctx
        a = await self.repo.get(alert_id)
        if a is None:
            raise KeyError(alert_id)
        if a.status != "sent":
            return a
        a.status = "accepted" if accept else "declined"
        a.responded_at = ctx.now().isoformat()
        await self.repo.put(a)
        inc = await ctx.incident_repo.get(a.incident_id)
        await ctx.audit.record(
            entity_type="volunteer_alert", entity_id=a.id,
            event_type="volunteer.accepted" if accept else "volunteer.declined", actor=actor,
            correlation_id=a.incident_id, source="simulated", payload={"volunteer_id": a.volunteer_id},
            vis=vis_for(inc) if inc else None)
        if accept:  # one responder is enough: stand the others down, openly
            for o in await self.list_for(a.incident_id):
                if o.id != a.id and o.status == "sent":
                    o.status, o.responded_at = "stood_down", ctx.now().isoformat()
                    await self.repo.put(o)
        return a

    async def _simulate_responses(self, incident_id: str, ids: list[str]) -> None:
        """Synthetic volunteers answer after a short delay: the nearest accepts, the rest decline."""
        await asyncio.sleep(3.0)
        for i, aid in enumerate(ids):
            await self.respond(aid, accept=(i == 0), actor="simulated-volunteer")
            await asyncio.sleep(1.0)
