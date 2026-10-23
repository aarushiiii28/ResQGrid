"""M2 last-100-metre access: choose an entrance, expire temporary reports, ask for confirmation
when uncertain."""

from datetime import timedelta

from app.core.config import Settings
from app.core.geo import haversine_m
from app.db.repos import Repo
from app.schemas.access import Building, Entrance, TempAccessReport
from app.schemas.common import Source
from app.schemas.routing import AccessReportCreate, EntranceCandidate, EntranceDecision
from app.services.audit_service import AuditService

MIN_CONFIDENT = 0.7
AMBIGUITY_MARGIN = 0.08


class AccessError(Exception):
    pass


class AccessService:
    def __init__(self, repo: Repo[Building], audit: AuditService, store, settings: Settings, now):
        self.repo, self.audit, self.store, self.settings, self.now = repo, audit, store, settings, now

    def _active(self, b: Building) -> tuple[list[TempAccessReport], list[str]]:
        now = self.now()
        act = [r for r in b.temp_reports if r.expires_at > now]
        return act, [r.id for r in b.temp_reports if r.expires_at <= now]

    def score_entrance(self, e: Entrance, reports: list[TempAccessReport],
                       blocked_ids: set[str], origin: tuple[float, float] | None) -> EntranceCandidate:
        now = self.now()
        s, why, usable = e.confidence, [], True
        if e.accessible:
            why.append("accessible")
        else:
            s -= 0.3
            why.append("not_step_free")
        if e.gate_restricted:
            s -= 0.15
            why.append("gate_restricted")
        if e.kind in ("emergency", "main"):
            s += 0.05
        if e.verified_at is None:
            s -= 0.15
            why.append("never_verified")
        else:
            age_d = (now - e.verified_at).days
            if age_d > 90:
                s -= 0.2
                why.append("verification_over_90_days")
            elif age_d > 30:
                s -= 0.1
                why.append("verification_over_30_days")
        mine = [r for r in reports if r.entrance_id == e.id]
        for r in mine:
            if r.kind in ("blocked", "gate_locked"):
                usable = False
                why.append(f"temp_report_{r.kind}")
            else:
                s += 0.2
                why.append(f"temp_report_{r.kind}")
        if e.id in blocked_ids:
            usable = False
            why.append("blocked_entrance_disruption")
        if origin is not None:
            s -= min(0.1, haversine_m(origin[0], origin[1], e.location.lat, e.location.lon) / 5000)
        return EntranceCandidate(entrance=e, score=round(s, 4), usable=usable, reasons=why,
                                 active_report_ids=[r.id for r in mine])

    def decide(self, b: Building, blocked_ids: set[str] | None = None,
               origin: tuple[float, float] | None = None) -> EntranceDecision:
        reports, expired = self._active(b)
        cands = [self.score_entrance(e, reports, blocked_ids or set(), origin) for e in b.entrances]
        ranked = sorted(cands, key=lambda c: (not c.usable, -c.score, c.entrance.id))
        usable = [c for c in ranked if c.usable]
        if not usable:
            return EntranceDecision(
                building_id=b.id, chosen=None, alternatives=ranked, needs_confirmation=True,
                confirmation_request=f"No usable entrance known for {b.name}. Ask caller or crew which entrance is open.",
                expired_report_ids=expired,
            )
        best = usable[0]
        ambiguous = len(usable) > 1 and best.score - usable[1].score < AMBIGUITY_MARGIN
        uncertain = best.score < MIN_CONFIDENT
        req = None
        if ambiguous or uncertain:
            alt = usable[1].entrance.name if len(usable) > 1 else None
            req = (f"Confirm entrance for {b.name}: {best.entrance.name}"
                   + (f" or {alt}" if alt else "") + ". Ask caller or crew.")
        return EntranceDecision(
            building_id=b.id, chosen=best, alternatives=[c for c in ranked if c is not best],
            needs_confirmation=ambiguous or uncertain, confirmation_request=req,
            expired_report_ids=expired,
        )

    async def add_report(self, building_id: str, body: AccessReportCreate, actor: str) -> TempAccessReport:
        now = self.now()
        n = await self.store.next_seq("access_report_seq")
        rep = TempAccessReport(
            id=f"ACC-{n:04d}", entrance_id=body.entrance_id, kind=body.kind, reported_at=now,
            expires_at=now + timedelta(seconds=self.settings.access_report_ttl_s),
            source=Source.reported, note=body.note,
        )

        def fn(b: Building) -> Building:
            if body.entrance_id not in {e.id for e in b.entrances}:
                raise AccessError(f"unknown entrance {body.entrance_id}")
            b.temp_reports.append(rep)
            return b

        await self.repo.modify(building_id, fn)
        await self.audit.record(
            entity_type="building", entity_id=building_id, event_type="access.report_added",
            actor=actor, source="reported", new=rep.model_dump(mode="json"),
        )
        return rep

    async def reconfirm(self, building_id: str, report_id: str, actor: str) -> TempAccessReport:
        now = self.now()
        out: dict = {}

        def fn(b: Building) -> Building:
            for r in b.temp_reports:
                if r.id == report_id:
                    if r.expires_at <= now:
                        raise AccessError("report already expired; file a new report")
                    r.reconfirmed_at = now
                    r.expires_at = now + timedelta(seconds=self.settings.access_report_ttl_s)
                    out["r"] = r
                    return b
            raise AccessError(f"unknown report {report_id}")

        await self.repo.modify(building_id, fn)
        await self.audit.record(
            entity_type="building", entity_id=building_id, event_type="access.report_reconfirmed",
            actor=actor, source="human_confirmed", new=out["r"].model_dump(mode="json"),
        )
        return out["r"]
