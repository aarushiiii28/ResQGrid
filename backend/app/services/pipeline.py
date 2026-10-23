"""Auto-pipeline: after the SOS the caller does nothing. Steps (each audited and pushed over the
WebSocket): incident update, dispatch recommendation, approval (Demo Autopilot, or a dispatcher),
hospital match, pre-alert. The first recommendation is sent immediately using the safest assumption
(unknown is not minor: advanced life support); extraction then refines it, and the switch penalty
keeps the plan stable unless a change clearly helps."""

import asyncio

from app.schemas.dispatch import Assignment, DispatchDecision
from app.schemas.incident import Incident
from app.services.dispatch_service import category_of
from app.services.incident_service import INACTIVE, RuleViolation, vis_for

STEPS = ["incident_update", "dispatch_recommendation", "approval", "hospital_match", "pre_alert"]


class PipelineService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.sig: dict[str, str] = {}
        self.waiting: set[str] = set()  # incidents in the intake wait

    async def step(self, inc: Incident, name: str, status: str, detail: str = "", **payload) -> None:
        await self.ctx.audit.record(
            entity_type="pipeline", entity_id=inc.id, event_type="pipeline.step", actor="pipeline",
            correlation_id=inc.id, source="simulated", payload={"step": name, "status": status, "detail": detail, **payload},
            vis=vis_for(inc))

    def start(self, incident_id: str) -> None:
        self.ctx.spawn(self._run(incident_id, "sos"))

    def on_extraction(self, incident_id: str, applied: list[str]) -> None:
        self.ctx.spawn(self._after_extraction(incident_id, applied))

    async def _after_extraction(self, incident_id: str, applied: list[str]) -> None:
        inc = await self.ctx.incident_repo.get(incident_id)
        if inc is None or inc.status in INACTIVE:
            return
        await self.step(inc, "extraction", "done", "fields from the call: " + ", ".join(applied), fields=applied)
        if incident_id in self.waiting:
            return  # the intake wait is running: it dispatches as soon as it sees the category
        pa = await self.ctx.cases.refresh(incident_id)
        if category_of(inc) != self.sig.get(incident_id) and self.sig.get(incident_id) is not None:
            await self._run(incident_id, "refined")
        elif pa is None and inc.assigned_vehicle_id is None and incident_id not in self.sig:
            await self._run(incident_id, "refined")

    async def _intake(self, incident_id: str) -> bool:
        """Understand the call before choosing an ambulance. The caller is asked what happened; as soon as the category is known the
        severity is predicted and the right type of unit is chosen. The wait is bounded (`intake_wait_s`): with no answer the unit is
        sent on the safest assumption (advanced life support) and refined when the caller answers. A crash signature or a call for help
        skips the wait. Returns False if the request was stopped (the caller said they are safe, or the incident ended)."""
        ctx = self.ctx
        wait = float(ctx.settings.intake_wait_s)
        inc = await ctx.incident_repo.get(incident_id)
        if wait <= 0 or inc is None or inc.reported_category is not None:
            return True
        fa = ctx.false_alarm
        rec = await fa.get(incident_id)
        if rec and (rec.lane == "HIGH" or rec.explicit_help):
            await self.step(inc, "intake", "done", "high-risk signal (for example a crash detected by the phone): not waiting for the description")
            return True
        await self.step(inc, "intake", "waiting", f"asking what happened (up to {wait:.0f} s), then the severity is predicted and the right ambulance is chosen")
        self.waiting.add(incident_id)
        deadline = ctx.now().timestamp() + wait
        try:
            while True:
                await asyncio.sleep(0.25)
                inc = await ctx.incident_repo.get(incident_id)
                if inc is None or inc.status in INACTIVE:
                    return False
                if not await fa.auto_dispatch_allowed(incident_id):
                    await self.step(inc, "intake", "stopped", "the caller said they are safe: no ambulance is being sent")
                    return False
                rec = await fa.get(incident_id)
                if inc.reported_category is not None:
                    cat = category_of(inc)
                    prio = inc.operator_priority.value if inc.operator_priority else None
                    await self.step(inc, "intake", "done", f"caller reported {cat.replace('_', ' ')}: predicted severity " + (f"P{prio}" if prio else "unknown")
                                    + ". Choosing the nearest suitable ambulance.", category=cat, priority=prio)
                    return True
                if rec and rec.explicit_help:
                    await self.step(inc, "intake", "done", "the caller asked for help: not waiting any longer")
                    return True
                if ctx.now().timestamp() >= deadline:
                    await self.step(inc, "intake", "done", f"no description after {wait:.0f} s: sending advanced life support on the safest assumption, "
                                    "and refining when the caller answers", timeout=True)
                    return True
        finally:
            self.waiting.discard(incident_id)

    async def _run(self, incident_id: str, why: str) -> None:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        if inc is None:
            return
        if why == "sos" and not await self._intake(incident_id):
            return
        if not await ctx.false_alarm.auto_dispatch_allowed(incident_id):
            return
        inc = await ctx.incident_repo.get(incident_id)
        self.sig[incident_id] = category_of(inc)
        await self.step(inc, "incident_update", "done",
                        "location received" if why == "sos" else "requirements changed after the call: re-checking")
        await self.step(inc, "dispatch_recommendation", "running")
        try:
            dec = await ctx.dispatch.recommend(incident_id, actor="pipeline")
        except RuleViolation as e:
            await self.step(inc, "dispatch_recommendation", "failed", str(e))
            return
        r = dec.recommendation
        if not await ctx.false_alarm.auto_dispatch_allowed(incident_id):  # the caller said they are safe while we were choosing
            dec.status = "superseded"
            await ctx.dispatch.repo.put(dec)
            await self.step(inc, "dispatch_recommendation", "stopped", "the caller said they are safe: nothing was assigned")
            return
        await self.step(inc, "dispatch_recommendation", "done", f"{r.vehicle_id} to {r.hospital_id}",
                        decision_id=dec.id, keep_current=dec.keep_current)
        if ctx.autopilot.enabled:
            await self.step(inc, "approval", "running", "Demo Autopilot: simulated dispatcher approval")
            task = ctx.autopilot.schedule(dec.id)
            if task:
                await task
        else:
            await self.step(inc, "approval", "waiting", "waiting for a dispatcher to approve")

    async def on_decision(self, dec: DispatchDecision, inc: Incident, assignment: Assignment | None, actor: str) -> None:
        """Called by dispatch.approve after a decision is final."""
        ctx = self.ctx
        if dec.status == "rejected":
            await self.step(inc, "approval", "rejected", f"rejected by {actor}")
            return
        await self.step(inc, "approval", "done",
                        "kept the current plan" if dec.status == "kept_current" else f"approved by {actor}", actor=actor)
        if dec.status == "kept_current":
            return
        f = dec.final or {}
        h = await ctx.hospital_repo.get(f.get("hospital_id")) if f.get("hospital_id") else None
        if h is not None:
            view = ctx.hospitals.view(h)
            stale = [k for k, r in view.resources.items() if r.stale and r.status == "unknown" and k in dec.inputs.get("hospitals", {}).get(h.id, {})]
            await self.step(inc, "hospital_match", "done", f"{h.name}" + (" (some capacity data unverified)" if stale else ""),
                            hospital_id=h.id)
        pa = await ctx.cases.for_incident(inc.id)
        if pa:
            await self.step(inc, "pre_alert", "done", f"pre-alert sent to {pa.hospital_id} (simulated)", prealert_id=pa.id)
        if assignment is not None:
            v_cat = category_of(inc)
            if v_cat in ("cardiac", "unconscious", "breathing"):
                await ctx.volunteers.alert(inc, "pipeline")
            ctx.demo_driver.start(assignment.id)
