from datetime import timedelta

from app.core.config import Settings
from app.db.pg import Store
from app.db.repos import Repo
from app.routing.base import DisruptionSet, RoutingProvider
from app.routing.mock import MockRoutingProvider
from app.routing.osrm import OSRMProvider
from app.schemas.routing import Disruption, DisruptionCreate, MatchResult, RouteEstimate
from app.services.audit_service import AuditService
from app.websockets.manager import EventHub


def build_provider(settings: Settings) -> RoutingProvider:
    if settings.routing_provider == "osrm":
        return OSRMProvider(settings.osrm_url, settings.osrm_timeout_s)
    return MockRoutingProvider()


class RoutingService:
    def __init__(self, store: Store, provider: RoutingProvider, audit: AuditService,
                 hub: EventHub, settings: Settings, now):
        self.repo = Repo(store, "disruption", Disruption)
        self.store, self.provider, self.audit, self.hub = store, provider, audit, hub
        self.settings, self.now = settings, now

    async def active_disruptions(self) -> DisruptionSet:
        now = self.now()
        return DisruptionSet([d for d in await self.repo.list() if d.active(now)])

    async def route(self, origin: tuple[float, float], dest: tuple[float, float],
                    disruptions: DisruptionSet | None = None) -> RouteEstimate:
        d = disruptions if disruptions is not None else await self.active_disruptions()
        return await self.provider.route(origin, dest, d, self.now())

    async def alternatives(self, origin: tuple[float, float], dest: tuple[float, float], k: int = 2,
                           disruptions: DisruptionSet | None = None) -> list[RouteEstimate]:
        d = disruptions if disruptions is not None else await self.active_disruptions()
        return await self.provider.alternatives(origin, dest, d, self.now(), k)

    async def match(self, points: list[tuple[float, float]], hints: list[list[tuple[float, float]]] | None = None,
                    radius_m: float = 25.0) -> MatchResult:
        return await self.provider.match(points, hints, radius_m)

    async def add_disruption(self, body: DisruptionCreate, actor: str) -> Disruption:
        if body.kind == "blocked_entrance":
            if not body.entrance_id:
                raise ValueError("entrance_id required for blocked_entrance")
        elif body.kind in ("closure", "event_zone") and (body.lat is None or body.lon is None or body.radius_m is None):
            raise ValueError(f"{body.kind} needs lat, lon and radius_m")
        elif body.kind == "congestion" and body.multiplier <= 1.0:
            raise ValueError("congestion needs multiplier > 1")
        now = self.now()
        n = await self.store.next_seq("disruption_seq")
        d = Disruption(
            id=f"DIS-{n:03d}", kind=body.kind, lat=body.lat, lon=body.lon, radius_m=body.radius_m,
            multiplier=body.multiplier if body.kind != "closure" else 1.0,
            entrance_id=body.entrance_id, note=body.note, created_by=actor, created_at=now,
            expires_at=now + timedelta(seconds=body.ttl_s) if body.ttl_s else None,
        )
        await self.repo.put(d)
        await self.audit.record(
            entity_type="disruption", entity_id=d.id, event_type="routing.disruption_added",
            actor=actor, source="reported", new=d.model_dump(mode="json"),
        )
        return d

    async def remove_disruption(self, disruption_id: str, actor: str, reason: str | None = None) -> bool:
        d = await self.repo.get(disruption_id)
        if d is None:
            return False
        await self.store.delete("disruption", disruption_id)
        await self.audit.record(
            entity_type="disruption", entity_id=disruption_id, event_type="routing.disruption_removed",
            actor=actor, reason=reason, old=d.model_dump(mode="json"),
        )
        return True
