"""M9 pre-alert and handover. The pre-alert separates what the caller reported from what has been
confirmed or measured, and lists unknowns. Arrival-to-handover time is simulated."""

from app.db.repos import Repo
from app.schemas.common import Source
from app.schemas.hospital_case import ChecklistItem, ChecklistUpdate, Observation, PreAlert
from app.schemas.incident import Incident
from app.schemas.vehicle import VehicleStateUpdate
from app.services.incident_service import RuleViolation

CATEGORY_LABEL = {
    "cardiac": "Cardiac", "stroke": "Stroke", "trauma": "Trauma", "neonatal": "Neonatal",
    "breathing": "Breathing difficulty", "unconscious": "Unresponsive person", "other": "Other",
}
UNKNOWN_LABEL = {
    "reported_category": "Reason for call not yet reported",
    "conscious": "Conscious state not known",
    "patient_count": "Number of patients not known",
    "exact_location": "Exact location not confirmed",
}
EXTRA_ITEMS = [("age", "Age (not collected in this prototype)"), ("allergies_meds", "Allergies and medicines (not collected)")]


class HospitalCaseService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[PreAlert] = Repo(ctx.store, "prealert", PreAlert)

    # ---- build -----------------------------------------------------------------
    def _content(self, inc: Incident) -> dict:
        now = self.ctx.now()
        confirmed, reported = [], []

        def add(key: str, prov) -> None:
            if prov is None:
                return
            o = Observation(key=key, value=prov.value, source=prov.source, at=prov.at, confidence=prov.confidence)
            (confirmed if prov.source in (Source.human_confirmed, Source.measured) else reported).append(o)

        add("reported_category", inc.reported_category)
        add("patient_count", inc.patient_count)
        for k, v in inc.facts.items():
            add(k, v)
        cat = inc.reported_category.value if inc.reported_category else None
        src = inc.reported_category.source.value if inc.reported_category else "not reported"
        reason = (f"{CATEGORY_LABEL.get(cat, cat)} ({src.replace('_', ' ')})" if cat
                  else "Reason not yet reported (unknown is not minor)")
        unknowns = [UNKNOWN_LABEL.get(u, u) for u in inc.unknowns]
        _ = now
        return {"reported_reason": reason, "reported_category": cat, "confirmed_observations": confirmed,
                "reported_observations": reported, "unknowns": unknowns}

    def _checklist(self, inc: Incident, existing: list[ChecklistItem]) -> list[ChecklistItem]:
        have = {c.key: c for c in existing}
        items = []
        for key in inc.unknowns:
            items.append(have.get(key) or ChecklistItem(key=key, label=UNKNOWN_LABEL.get(key, key)))
        for key, label in EXTRA_ITEMS:
            items.append(have.get(key) or ChecklistItem(key=key, label=label))
        # an item that is now known becomes "provided" automatically, with the source noted
        for it in items:
            if it.key not in inc.unknowns and it.status == "open" and it.key in ("reported_category", "conscious", "patient_count", "exact_location"):
                it.status, it.note = "provided", "now known from the call"
        for key in ("reported_category", "conscious", "patient_count"):
            if key not in inc.unknowns and key not in {i.key for i in items}:
                known = have.get(key) or ChecklistItem(key=key, label=UNKNOWN_LABEL.get(key, key))
                if known.status == "open":
                    known.status, known.note = "provided", "now known from the call"
                items.append(known)
        return items

    async def for_incident(self, incident_id: str) -> PreAlert | None:
        live = [p for p in await self.repo.list() if p.incident_id == incident_id and p.status != "superseded"]
        return live[-1] if live else None

    async def upsert_for_incident(self, inc: Incident, vehicle_id: str, hospital_id: str,
                                  eta: tuple[float, float] | None, actor: str) -> PreAlert:
        ctx = self.ctx
        now = ctx.now()
        cur = await self.for_incident(inc.id)
        if cur and cur.hospital_id == hospital_id:
            return await self.refresh(inc.id, vehicle_id=vehicle_id, eta=eta)
        if cur:  # destination changed: the old hospital sees an explicit redirect, never silence
            old_h = cur.hospital_id
            cur.status, cur.superseded_reason, cur.updated_at = "superseded", f"destination changed to {hospital_id}", now
            await self.repo.put(cur)
            await ctx.audit.record(entity_type="prealert", entity_id=cur.id, event_type="prealert.superseded", actor=actor,
                                   correlation_id=inc.id, source="human_confirmed", reason=cur.superseded_reason,
                                   vis={"hospital_id": old_h})
        n = await ctx.store.next_seq("prealert_seq")
        c = self._content(inc)
        pa = PreAlert(id=f"PA-{n:04d}", incident_id=inc.id, hospital_id=hospital_id, vehicle_id=vehicle_id,
                      created_at=now, updated_at=now, eta_low_s=eta[0] if eta else None,
                      eta_high_s=eta[1] if eta else None, eta_updated_at=now if eta else None,
                      checklist=self._checklist(inc, []), **c)
        await self.repo.put(pa)
        await ctx.audit.record(
            entity_type="prealert", entity_id=pa.id, event_type="prealert.sent", actor=actor, correlation_id=inc.id,
            source="simulated", payload={"hospital_id": hospital_id, "vehicle_id": vehicle_id,
                                         "eta_low_s": pa.eta_low_s, "eta_high_s": pa.eta_high_s,
                                         "note": "simulated pre-alert; no real hospital integration"},
            vis={"hospital_id": hospital_id, "caller": inc.created_by})
        return pa

    async def upsert_mci(self, inc: Incident, hospital_id: str, summary: str, vehicle_ids: list[str],
                         eta: tuple[float, float] | None, actor: str) -> PreAlert:
        """Mass-casualty pre-alert: one per receiving hospital. Unlike a normal plan it never
        supersedes another hospital's pre-alert for the same incident."""
        ctx = self.ctx
        now = ctx.now()
        c = self._content(inc)
        c["reported_reason"] = f"Mass casualty: {summary}"
        existing = [p for p in await self.repo.list() if p.incident_id == inc.id and p.hospital_id == hospital_id and p.status != "superseded"]
        if existing:
            def fn(p: PreAlert) -> PreAlert:
                for k, v in c.items():
                    setattr(p, k, v)
                if eta:
                    p.eta_low_s, p.eta_high_s, p.eta_updated_at = eta[0], eta[1], now
                p.updated_at = now
                return p

            pa, _ = await self.repo.modify(existing[0].id, fn)
            return pa
        n = await ctx.store.next_seq("prealert_seq")
        pa = PreAlert(id=f"PA-{n:04d}", incident_id=inc.id, hospital_id=hospital_id, vehicle_id=",".join(vehicle_ids) or "MULTIPLE",
                      created_at=now, updated_at=now, eta_low_s=eta[0] if eta else None, eta_high_s=eta[1] if eta else None,
                      eta_updated_at=now if eta else None, checklist=self._checklist(inc, []), **c)
        await self.repo.put(pa)
        await ctx.audit.record(entity_type="prealert", entity_id=pa.id, event_type="prealert.sent", actor=actor, correlation_id=inc.id,
                               source="simulated", payload={"hospital_id": hospital_id, "mass_casualty": True, "summary": summary,
                                                            "note": "simulated pre-alert; no real hospital integration"},
                               vis={"hospital_id": hospital_id})
        return pa

    async def refresh(self, incident_id: str, vehicle_id: str | None = None,
                      eta: tuple[float, float] | None = None) -> PreAlert | None:
        ctx = self.ctx
        pa = await self.for_incident(incident_id)
        inc = await ctx.incident_repo.get(incident_id)
        if pa is None or inc is None:
            return pa
        c = self._content(inc)

        def fn(p: PreAlert) -> PreAlert:
            for k, v in c.items():
                setattr(p, k, v)
            if vehicle_id:
                p.vehicle_id = vehicle_id
            if eta:
                p.eta_low_s, p.eta_high_s, p.eta_updated_at = eta[0], eta[1], ctx.now()
            p.checklist = self._checklist(inc, p.checklist)
            p.updated_at = ctx.now()
            return p

        pa, _ = await self.repo.modify(pa.id, fn)
        await ctx.audit.record(
            entity_type="prealert", entity_id=pa.id, event_type="prealert.updated", actor="system",
            correlation_id=incident_id, source="simulated", payload={"reported_reason": pa.reported_reason},
            vis={"hospital_id": pa.hospital_id, "caller": inc.created_by})
        return pa

    async def set_eta(self, incident_id: str, low_s: float, high_s: float) -> None:
        pa = await self.for_incident(incident_id)
        if pa is None or pa.status in ("arrived", "handover_started", "handover_completed"):
            return

        def fn(p: PreAlert) -> PreAlert:
            p.eta_low_s, p.eta_high_s, p.eta_updated_at = low_s, high_s, self.ctx.now()
            return p

        pa, _ = await self.repo.modify(pa.id, fn)
        self.ctx.hub.publish("prealert.eta_updated", pa.id, correlation_id=incident_id, source="simulated",
                             payload={"eta_low_s": low_s, "eta_high_s": high_s},
                             vis={"hospital_id": pa.hospital_id})

    # ---- hospital workflow --------------------------------------------------------
    async def _get(self, pid: str) -> PreAlert:
        pa = await self.repo.get(pid)
        if pa is None:
            raise KeyError(pid)
        return pa

    async def _transition(self, pid: str, event: str, actor: str, fn, reason: str | None = None) -> PreAlert:
        pa = await self._get(pid)
        old = pa.status
        pa, _ = await self.repo.modify(pid, fn)
        inc = await self.ctx.incident_repo.get(pa.incident_id)
        await self.ctx.audit.record(
            entity_type="prealert", entity_id=pa.id, event_type=event, actor=actor, correlation_id=pa.incident_id,
            source="human_confirmed" if not actor.startswith("sim") else "simulated", reason=reason, old=old, new=pa.status,
            payload={"hospital_id": pa.hospital_id, "handover_duration_s": pa.handover_duration_s},
            vis={"hospital_id": pa.hospital_id, "caller": inc.created_by if inc else None})
        return pa

    async def acknowledge(self, pid: str, actor: str) -> PreAlert:
        def fn(p: PreAlert) -> PreAlert:
            if p.status == "superseded":
                raise RuleViolation("pre-alert was superseded; the patient is going elsewhere")
            if p.acknowledged_at is None:
                p.acknowledged_at, p.acknowledged_by = self.ctx.now(), actor
                if p.status == "incoming":
                    p.status = "acknowledged"
            return p

        return await self._transition(pid, "prealert.acknowledged", actor, fn)

    async def arrival(self, pid: str, actor: str, source: str = "hospital") -> PreAlert:
        def fn(p: PreAlert) -> PreAlert:
            if p.status == "superseded":
                raise RuleViolation("pre-alert was superseded")
            if p.arrived_at is None:
                p.arrived_at, p.arrival_source = self.ctx.now(), source
                if p.status in ("incoming", "acknowledged"):
                    p.status = "arrived"
            return p

        return await self._transition(pid, "prealert.arrived", actor, fn)

    async def handover_started(self, pid: str, actor: str) -> PreAlert:
        def fn(p: PreAlert) -> PreAlert:
            if p.arrived_at is None:
                raise RuleViolation("arrival must be recorded before handover can start")
            if p.handover_started_at is None:
                p.handover_started_at, p.status = self.ctx.now(), "handover_started"
            return p

        return await self._transition(pid, "prealert.handover_started", actor, fn)

    async def handover_completed(self, pid: str, actor: str) -> PreAlert:
        def fn(p: PreAlert) -> PreAlert:
            if p.handover_started_at is None:
                raise RuleViolation("handover must be started before it can be completed")
            if p.handover_completed_at is None:
                now = self.ctx.now()
                p.handover_completed_at, p.status = now, "handover_completed"
                p.handover_duration_s = round((now - p.arrived_at).total_seconds(), 1)
            return p

        pa = await self._transition(pid, "prealert.handover_completed", actor, fn)
        ctx = self.ctx
        v = await ctx.vehicle_repo.get(pa.vehicle_id)
        if v and v.status in ("transporting", "on_scene"):
            await ctx.vehicles.apply_update(pa.vehicle_id, VehicleStateUpdate(
                update_id=f"{pa.id}-free", occurred_at=ctx.now(), status="available",
                reason="handover completed", source="reported"), actor)
        inc = await ctx.incident_repo.get(pa.incident_id)
        if inc and inc.status != "closed":
            def close(i: Incident) -> Incident:
                i.status = "closed"
                i.updated_at = ctx.now()
                return i

            inc, _ = await ctx.incident_repo.modify(inc.id, close)
            await ctx.audit.record(entity_type="incident", entity_id=inc.id, event_type="incident.closed", actor=actor,
                                   correlation_id=inc.id, source="human_confirmed", reason="handover completed",
                                   vis={"caller": inc.created_by, "hospital_id": pa.hospital_id})
        return pa

    async def update_checklist(self, pid: str, body: ChecklistUpdate, actor: str) -> PreAlert:
        def fn(p: PreAlert) -> PreAlert:
            for it in p.checklist:
                if it.key == body.key:
                    it.status, it.note, it.updated_by, it.updated_at = body.status, body.note, actor, self.ctx.now()
                    return p
            raise RuleViolation(f"unknown checklist item {body.key}")

        return await self._transition(pid, "prealert.checklist_updated", actor, fn, reason=f"{body.key}: {body.status}")
