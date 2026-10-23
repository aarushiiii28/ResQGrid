from typing import Any

from app.core.config import Settings
from app.core.security import Principal
from app.db.repos import Repo
from app.schemas.common import SOURCE_RANK, GeoPoint, Prov, Source
from app.schemas.incident import (
    Conflict,
    Incident,
    IncidentCreate,
    LocationCandidate,
    MergeRecord,
    Report,
    ReportCreate,
)
from app.services.audit_service import AuditService
from app.services.duplicate import suggest_duplicates

TOP_FIELDS = ("reported_category", "operator_priority", "patient_count")
# no further work is done on these; "pending" (countdown running) is also not dispatchable
INACTIVE = ("closed", "merged", "cancelled_by_caller")
NOT_DISPATCHABLE = (*INACTIVE, "pending")


class RuleViolation(Exception):
    """A business rule was broken (mapped to HTTP 409/422 by the API layer)."""


def can_view(p: Principal, inc: Incident) -> bool:
    if p.role in ("dispatcher", "admin"):
        return True
    if p.role == "caller":
        return inc.created_by == p.sub
    if p.role == "driver":
        return p.vehicle_id is not None and inc.assigned_vehicle_id == p.vehicle_id
    if p.role == "hospital":
        return p.hospital_id is not None and inc.destination_id == p.hospital_id
    return False


def vis_for(inc: Incident) -> dict:
    return {
        "caller": inc.created_by,
        "vehicle_id": inc.assigned_vehicle_id,
        "hospital_id": inc.destination_id,
    }


def merge_field(existing: Prov | None, new: Prov, key: str, now) -> tuple[Prov, Conflict | None]:
    """Field-level precedence: higher-ranked source wins; lower/equal never silently overwrite
    a different value. Disagreements are recorded as conflicts."""
    if existing is None:
        return new, None
    if existing.value == new.value:
        keep_new = SOURCE_RANK[new.source] >= SOURCE_RANK[existing.source]
        return (new if keep_new else existing), None
    if SOURCE_RANK[new.source] > SOURCE_RANK[existing.source]:
        c = Conflict(
            field=key, existing=existing.value, incoming=new.value,
            existing_source=existing.source, incoming_source=new.source,
            detected_at=now, resolved=True,
        )
        return new, c
    c = Conflict(
        field=key, existing=existing.value, incoming=new.value,
        existing_source=existing.source, incoming_source=new.source,
        detected_at=now, resolved=False,
    )
    return existing, c


def recompute_unknowns(inc: Incident) -> None:
    u = []
    if inc.reported_category is None:
        u.append("reported_category")
    if "conscious" not in inc.facts:
        u.append("conscious")
    if inc.patient_count is None:
        u.append("patient_count")
    if not inc.location_candidates or all(
        c.approximate or (c.accuracy_m or 0) > 50 for c in inc.location_candidates
    ):
        u.append("exact_location")
    inc.unknowns = u


class IncidentService:
    def __init__(self, repo: Repo[Incident], reports: Repo[Report], store, audit: AuditService,
                 settings: Settings, now):
        self.repo, self.reports, self.store = repo, reports, store
        self.audit, self.settings, self.now = audit, settings, now

    async def reports_for(self, incident_id: str) -> list[Report]:
        return [r for r in await self.reports.list() if r.incident_id == incident_id]

    async def _suggest(self, inc: Incident, text: str) -> list:
        others = []
        for o in await self.repo.list():
            if o.id == inc.id:
                continue
            txt = " ".join(r.text for r in await self.reports_for(o.id))
            others.append((o, txt))
        loc = inc.location_candidates[0] if inc.location_candidates else None
        return suggest_duplicates(
            created_at=inc.created_at, location=loc, text=text, others=others, settings=self.settings
        )

    async def create(self, body: IncidentCreate, p: Principal) -> Incident:
        now = self.now()
        human = p.role in ("dispatcher", "admin")
        src = Source.human_confirmed if human else Source.reported

        def prov(v: Any) -> Prov:
            return Prov(value=v, source=src, at=now, human_confirmed=human)

        n = await self.store.next_seq("incident_seq")
        inc = Incident(id=f"INC-{n}", created_at=now, created_by=p.sub, building_id=body.building_id)
        if body.reported_category:
            inc.reported_category = prov(body.reported_category)
        if body.operator_priority:
            inc.operator_priority = prov(body.operator_priority)
        if body.patient_count is not None:
            inc.patient_count = prov(body.patient_count)
        if body.location:
            loc = body.location.model_copy(update={"at": body.location.at or now})
            inc.location_candidates.append(
                LocationCandidate(**loc.model_dump(), label=body.location_label,
                                  building_id=body.building_id)
            )
        for k, v in body.facts.items():
            inc.facts[k] = Prov(value=v, source=Source.reported, at=now)
        recompute_unknowns(inc)
        inc.updated_at = now

        text = body.text or ""
        if text:
            await self._store_report(inc.id, text, body.channel, p.sub)
        inc.duplicate_suggestions = await self._suggest(inc, text)
        await self.repo.put(inc)

        await self.audit.record(
            entity_type="incident", entity_id=inc.id, event_type="incident.created",
            actor=p.sub, correlation_id=inc.id, source=src.value,
            new=inc.model_dump(mode="json", include={"status", "reported_category", "operator_priority",
                                                      "patient_count", "location_candidates"}),
            vis=vis_for(inc),
        )
        for s in inc.duplicate_suggestions:
            await self.audit.record(
                entity_type="incident", entity_id=inc.id, event_type="incident.duplicate_suggested",
                actor="system", correlation_id=inc.id, source="system",
                payload=s.model_dump(mode="json"), vis=vis_for(inc),
            )
        return inc

    async def _store_report(self, incident_id: str, text: str, channel: str, reporter: str) -> Report:
        n = await self.store.next_seq("report_seq")
        r = Report(id=f"REP-{n:04d}", incident_id=incident_id, text=text, channel=channel,
                   reporter=reporter, received_at=self.now())
        await self.reports.put(r)
        return r

    async def get(self, incident_id: str) -> Incident | None:
        return await self.repo.get(incident_id)

    async def list_for(self, p: Principal) -> list[Incident]:
        return [i for i in await self.repo.list() if can_view(p, i)]

    async def add_report(self, incident_id: str, body: ReportCreate, p: Principal):
        inc = await self.repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if inc.status in INACTIVE:
            raise RuleViolation(f"incident is {inc.status}")
        report = await self._store_report(incident_id, body.text, body.channel, p.sub)
        text = " ".join(r.text for r in await self.reports_for(incident_id))

        def fn(i: Incident) -> Incident:
            if body.location:
                loc = body.location.model_copy(update={"at": body.location.at or self.now()})
                i.location_candidates.append(LocationCandidate(**loc.model_dump()))
            i.duplicate_suggestions = [
                s for s in i.duplicate_suggestions if s.status != "suggested"
            ]
            recompute_unknowns(i)
            i.updated_at = self.now()
            return i

        inc, _ = await self.repo.modify(incident_id, fn)
        fresh = await self._suggest(inc, text)
        known = {s.incident_id: s for s in inc.duplicate_suggestions}
        merged = [s for s in fresh if known.get(s.incident_id) is None or known[s.incident_id].status == "suggested"]

        def save(i: Incident) -> Incident:
            i.duplicate_suggestions = [s for s in i.duplicate_suggestions if s.status != "suggested"] + merged
            return i

        inc, _ = await self.repo.modify(incident_id, save)
        await self.audit.record(
            entity_type="incident", entity_id=incident_id, event_type="incident.report_attached",
            actor=p.sub, correlation_id=incident_id, source="reported",
            payload={"report_id": report.id, "channel": body.channel}, vis=vis_for(inc),
        )
        for s in merged:
            await self.audit.record(
                entity_type="incident", entity_id=incident_id,
                event_type="incident.duplicate_suggested", actor="system",
                correlation_id=incident_id, source="system", payload=s.model_dump(mode="json"),
                vis=vis_for(inc),
            )
        return report, merged

    async def set_fact(self, incident_id: str, key: str, value: Any, *, source: Source,
                       confidence: float, actor: str, evidence: str | None = None,
                       reason: str | None = None) -> Incident:
        now = self.now()
        new = Prov(value=value, source=source, at=now, confidence=confidence, evidence=evidence,
                   human_confirmed=source == Source.human_confirmed)
        snapshot: dict = {}

        def fn(i: Incident) -> Incident:
            old = getattr(i, key, None) if key in TOP_FIELDS else i.facts.get(key)
            if key == "operator_priority" and old is not None and source in (
                Source.ai_extracted, Source.simulated
            ) and isinstance(value, int) and value > old.value:
                raise RuleViolation("automated sources cannot downgrade urgency")
            snapshot["old"] = None if old is None else old.model_dump(mode="json")
            winner, conflict = merge_field(old, new, key, now)
            if key in TOP_FIELDS:
                setattr(i, key, winner)
            else:
                i.facts[key] = winner
            if conflict:
                i.conflicts.append(conflict)
            if source == Source.human_confirmed:
                for c in i.conflicts:
                    if c.field == key:
                        c.resolved = True
            snapshot["new"] = winner.model_dump(mode="json")
            snapshot["conflict"] = conflict is not None and not conflict.resolved
            recompute_unknowns(i)
            i.updated_at = now
            return i

        inc, _ = await self.repo.modify(incident_id, fn)
        await self.audit.record(
            entity_type="incident", entity_id=incident_id,
            event_type="incident.fact_conflict" if snapshot["conflict"] else "incident.fact_set",
            actor=actor, correlation_id=incident_id, source=source.value, reason=reason,
            old=snapshot["old"], new=snapshot["new"], payload={"field": key}, vis=vis_for(inc),
        )
        return inc

    async def assign(self, incident_id: str, vehicle_id: str | None, destination_id: str | None,
                     actor: str, reason: str | None = None) -> Incident:
        before: dict = {}

        def fn(i: Incident) -> Incident:
            before.update(vehicle=i.assigned_vehicle_id, destination=i.destination_id, status=i.status)
            if vehicle_id is not None:
                i.assigned_vehicle_id = vehicle_id
            if destination_id is not None:
                i.destination_id = destination_id
            if i.status == "new":
                i.status = "dispatched"
            i.updated_at = self.now()
            return i

        inc, _ = await self.repo.modify(incident_id, fn)
        await self.audit.record(
            entity_type="incident", entity_id=incident_id, event_type="incident.assigned",
            actor=actor, correlation_id=incident_id, reason=reason, old=before,
            new={"vehicle": inc.assigned_vehicle_id, "destination": inc.destination_id,
                 "status": inc.status},
            vis=vis_for(inc),
        )
        return inc

    # ---- reversible merge / split ----------------------------------
    async def merge(self, source_id: str, target_id: str, actor: str, reason: str) -> Incident:
        if source_id == target_id:
            raise RuleViolation("cannot merge an incident into itself")
        src, tgt = await self.repo.get(source_id), await self.repo.get(target_id)
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else target_id)
        if src.status == "merged" or tgt.status in (*INACTIVE, "pending"):
            raise RuleViolation("source already merged or target not open")
        now = self.now()
        moved = []
        for r in await self.reports_for(source_id):
            r.original_incident_id = r.original_incident_id or source_id
            r.incident_id = target_id
            await self.reports.put(r)
            moved.append(r.id)
        added: list[str] = []

        def to_target(t: Incident) -> Incident:
            for k, v in src.facts.items():
                if k not in t.facts:
                    t.facts[k] = v.model_copy(update={"evidence": f"merged from {source_id}"})
                    added.append(k)
            t.merged_from.append(source_id)
            t.merge_log[source_id] = MergeRecord(fact_keys=added, report_ids=moved, merged_at=now)
            for s in t.duplicate_suggestions:
                if s.incident_id == source_id:
                    s.status = "merged"
            t.updated_at = now
            return t

        def to_source(s: Incident) -> Incident:
            s.status_before_merge = s.status
            s.status = "merged"
            s.merged_into = target_id
            s.updated_at = now
            return s

        tgt, _ = await self.repo.modify(target_id, to_target)
        await self.repo.modify(source_id, to_source)
        await self.audit.record(
            entity_type="incident", entity_id=target_id, event_type="incident.merged",
            actor=actor, correlation_id=target_id, source="human_confirmed", reason=reason,
            payload={"source_id": source_id, "report_ids": moved, "fact_keys": added},
            vis=vis_for(tgt),
        )
        return tgt

    async def unmerge(self, source_id: str, actor: str, reason: str) -> Incident:
        src = await self.repo.get(source_id)
        if src is None:
            raise KeyError(source_id)
        if src.status != "merged" or not src.merged_into:
            raise RuleViolation("incident is not merged")
        target_id = src.merged_into
        tgt = await self.repo.get(target_id)
        rec = tgt.merge_log.get(source_id) if tgt else None
        for r in await self.reports.list():
            if r.original_incident_id == source_id and r.incident_id == target_id:
                r.incident_id, r.original_incident_id = source_id, None
                await self.reports.put(r)

        def to_target(t: Incident) -> Incident:
            for k in (rec.fact_keys if rec else []):
                t.facts.pop(k, None)
            t.merged_from = [x for x in t.merged_from if x != source_id]
            t.merge_log.pop(source_id, None)
            for s in t.duplicate_suggestions:
                if s.incident_id == source_id and s.status == "merged":
                    s.status = "suggested"
            t.updated_at = self.now()
            return t

        def to_source(s: Incident) -> Incident:
            s.status = s.status_before_merge or "new"  # type: ignore[assignment]
            s.status_before_merge = None
            s.merged_into = None
            s.updated_at = self.now()
            return s

        if tgt:
            await self.repo.modify(target_id, to_target)
        src, _ = await self.repo.modify(source_id, to_source)
        await self.audit.record(
            entity_type="incident", entity_id=source_id, event_type="incident.unmerged",
            actor=actor, correlation_id=source_id, source="human_confirmed", reason=reason,
            payload={"was_merged_into": target_id}, vis=vis_for(src),
        )
        return src

    async def split(self, incident_id: str, report_ids: list[str], actor: str, reason: str) -> Incident:
        inc = await self.repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        mine = {r.id: r for r in await self.reports_for(incident_id)}
        if not set(report_ids) <= set(mine):
            raise RuleViolation("report_ids must belong to the incident")
        if len(report_ids) == len(mine):
            raise RuleViolation("cannot split off every report")
        now = self.now()
        n = await self.store.next_seq("incident_seq")
        new = Incident(id=f"INC-{n}", created_at=now, created_by=inc.created_by,
                       split_from=incident_id, location_candidates=list(inc.location_candidates[:1]))
        recompute_unknowns(new)
        await self.repo.put(new)
        for rid in report_ids:
            r = mine[rid]
            r.original_incident_id = r.original_incident_id or incident_id
            r.incident_id = new.id
            await self.reports.put(r)
        await self.audit.record(
            entity_type="incident", entity_id=new.id, event_type="incident.split",
            actor=actor, correlation_id=new.id, source="human_confirmed", reason=reason,
            payload={"from": incident_id, "report_ids": report_ids}, vis=vis_for(new),
        )
        await self.audit.record(
            entity_type="incident", entity_id=incident_id, event_type="incident.split_out",
            actor=actor, correlation_id=incident_id, source="human_confirmed", reason=reason,
            payload={"new_incident": new.id, "report_ids": report_ids}, vis=vis_for(inc),
        )
        return new


def location_summary(g: GeoPoint) -> dict:
    return {"lat": g.lat, "lon": g.lon, "accuracy_m": g.accuracy_m}
