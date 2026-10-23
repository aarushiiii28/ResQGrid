"""M8 risk monitor and recovery planner.

Detects eight kinds of trouble on live plans, turns each into one deduplicated risk with evidence and
options, and applies an option only after a human approves it (Demo Autopilot may approve, audited as
`demo-autopilot`). "Keep current plan" is always an option. Plan changes need a clear benefit unless
the trigger makes the current plan unsafe; a raised ETA-drift risk uses a lower clear threshold
(hysteresis) so noisy ETAs cannot flip the plan back and forth. Nothing is cancelled silently."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core.capabilities import CAP_RESOURCE, CATEGORY_HOSPITAL_CAPS, CATEGORY_VEHICLE_TAGS, vehicle_satisfies
from app.db.repos import Repo
from app.optimization.scoring import weights_from
from app.routing.geometry import tail_from
from app.schemas.dispatch import ApproveRequest, Candidate, DispatchDecision
from app.schemas.incident import Incident
from app.schemas.risk import (
    ConfirmationRequest,
    Effects,
    Evidence,
    RecoveryOption,
    Risk,
)
from app.schemas.vehicle import VehicleStateUpdate
from app.services.dispatch_service import category_of
from app.services.incident_service import RuleViolation, vis_for
from app.services.nav_service import slow_factor
from app.services.vehicle_service import make_view as vehicle_view

BUSY = ("assigned", "accepted", "en_route", "on_scene", "transporting")
OPEN_STATUSES = ("new", "dispatched", "active")
CONFIRM_FIRST = ("stale_vehicle_gps", "stale_hospital_resource", "state_conflict")
ACTOR = "risk-monitor"
AUTOPILOT = "demo-autopilot"


@dataclass
class Detection:
    type: str
    incident_id: str
    entity: str
    severity: str
    assumption: str
    evidence: list[Evidence] = field(default_factory=list)
    necessary: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.type}:{self.incident_id}:{self.entity}"


def pri(inc: Incident) -> int:
    return int(inc.operator_priority.value) if inc.operator_priority else 2


def mid(c: Candidate) -> float:
    return (c.eta_total_low_s + c.eta_total_high_s) / 2


class RiskService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[Risk] = Repo(ctx.store, "risk", Risk)

    # ---- queries ------------------------------------------------------------------------
    async def list(self, status: str | None = None, incident_id: str | None = None) -> list[Risk]:
        out = await self.repo.list()
        return [r for r in out if (status is None or r.status == status) and (incident_id is None or r.incident_id == incident_id)]

    async def get(self, risk_id: str) -> Risk:
        r = await self.repo.get(risk_id)
        if r is None:
            raise KeyError(risk_id)
        return r

    async def open_risks(self) -> list[Risk]:
        return [r for r in await self.repo.list() if r.status in ("open", "awaiting")]

    # ---- detection ------------------------------------------------------------------------
    async def _detect(self, open_by_key: dict[str, Risk]) -> list[Detection]:
        ctx, s = self.ctx, self.ctx.settings
        now = ctx.now()
        dets: list[Detection] = []
        incs = [i for i in await ctx.incident_repo.list() if i.status in OPEN_STATUSES]
        disr = await ctx.routing.active_disruptions()
        for inc in incs:
            if not inc.assigned_vehicle_id:
                continue
            v = await ctx.vehicle_repo.get(inc.assigned_vehicle_id)
            if v is None:
                continue
            asg = [a for a in await ctx.acks.list(inc.id) if a.vehicle_id == v.id]
            a = asg[-1] if asg else None
            view = vehicle_view(v, now, s.vehicle_stale_s)

            # 1. stale vehicle GPS (shown as last known, never live)
            if v.status in BUSY and (view.location_state == "stale" or v.offline):
                dets.append(Detection(
                    "stale_vehicle_gps", inc.id, v.id, "critical" if v.status in ("en_route", "transporting") else "high",
                    f"{v.id} is where its last report says it is and is still able to respond",
                    [Evidence(key="location_age_s", value=view.location_age_s, at=v.location_at),
                     Evidence(key="offline", value=v.offline), Evidence(key="status", value=v.status)]))

            # 2. no acknowledgment (sent is not received)
            if a and (a.state == "timed_out" or (a.state in ("sent", "delivered_if_known") and a.sent_at
                                                 and (now - a.sent_at).total_seconds() >= a.timeout_s)):
                dets.append(Detection(
                    "no_acknowledgment", inc.id, v.id, "high", f"{v.id} has received and accepted the assignment",
                    [Evidence(key="assignment_state", value=a.state, at=a.sent_at),
                     Evidence(key="waited_s", value=round((now - a.sent_at).total_seconds()) if a.sent_at else None),
                     Evidence(key="timeout_s", value=a.timeout_s)], necessary=True))

            # 3. route closure and 6. ETA drift on the active route
            if v.status in ("accepted", "en_route", "transporting") and v.location is not None:
                try:
                    plan = await ctx.nav.plan(v.id)
                except Exception:  # noqa: BLE001
                    plan = None
                if plan is not None:
                    pl = [tuple(p) for p in plan.route.polyline]
                    tail = tail_from(pl, plan.along_m)
                    hits = disr.hit_closures(tail)
                    if hits:
                        dets.append(Detection(
                            "route_closure", inc.id, v.id, "high", "the active route to the target is open",
                            [Evidence(key="closures", value=[c.id for c in hits]),
                             Evidence(key="remaining_m", value=plan.remaining_m)], necessary=True))
                    factor = slow_factor(tail, disr)
                    drift_s = plan.remaining_s - plan.remaining_s / factor if factor else 0
                    key = f"eta_drift:{inc.id}:{v.id}"
                    thresh = s.eta_clear_factor if key in open_by_key else s.eta_raise_factor  # hysteresis
                    if factor >= thresh and drift_s >= (s.eta_min_delta_s if key not in open_by_key else 1):
                        dets.append(Detection(
                            "eta_drift", inc.id, v.id, "medium", "the ETA given to the caller and hospital still holds",
                            [Evidence(key="slowdown_factor", value=round(factor, 2)),
                             Evidence(key="extra_s", value=round(drift_s)),
                             Evidence(key="raise_threshold", value=s.eta_raise_factor),
                             Evidence(key="clear_threshold", value=s.eta_clear_factor)]))

            # 4/5. destination hospital: stale resource, capacity change, capability change
            if inc.destination_id:
                h = await ctx.hospital_repo.get(inc.destination_id)
                if h is not None:
                    cat = category_of(inc)
                    caps = CATEGORY_HOSPITAL_CAPS[cat]
                    if not caps <= set(h.capabilities):
                        dets.append(Detection(
                            "capability_change", inc.id, h.id, "critical", f"{h.id} can treat this patient",
                            [Evidence(key="missing", value=sorted(caps - set(h.capabilities)))], necessary=True))
                    hv = ctx.hospitals.view(h)
                    for cap in sorted(caps):
                        rname = CAP_RESOURCE.get(cap)
                        rv = hv.resources.get(rname) if rname else None
                        if rv is None:
                            continue
                        if rv.status == "unavailable":
                            dets.append(Detection(
                                "capacity_change", inc.id, h.id, "high", f"{h.id} has {rname} ready for this patient",
                                [Evidence(key="resource", value=rname), Evidence(key="status", value="unavailable"),
                                 Evidence(key="age_s", value=rv.age_s)], necessary=True))
                        elif rv.status == "unknown":
                            dets.append(Detection(
                                "stale_hospital_resource", inc.id, h.id, "medium", f"{h.id} has {rname} ready for this patient",
                                [Evidence(key="resource", value=rname), Evidence(key="reported", value=rv.reported_status),
                                 Evidence(key="age_s", value=rv.age_s), Evidence(key="stale", value=rv.stale)]))

            # 7. state conflict: the three records disagree
            problems = []
            if v.assigned_incident_id != inc.id:
                problems.append(f"vehicle record says incident {v.assigned_incident_id}")
            if v.status == "available":
                problems.append("vehicle reports available")
            if a and a.state == "rejected":
                problems.append("assignment was rejected")
            if problems:
                dets.append(Detection(
                    "state_conflict", inc.id, v.id, "high", "the incident, vehicle and assignment records agree",
                    [Evidence(key="incident_says", value=inc.assigned_vehicle_id), Evidence(key="problems", value=problems)]))

        # 8. competing incident: a more urgent call has no free unit but a less urgent one holds a suitable unit
        for u in incs:
            if u.assigned_vehicle_id or not u.location_candidates:
                continue
            try:
                ev = await ctx.dispatch.evaluate(u, weights=weights_from(s))
            except RuleViolation:
                continue
            if ev["cands"]:
                continue
            req = CATEGORY_VEHICLE_TAGS[category_of(u)]
            for c in incs:
                if c.id == u.id or not c.assigned_vehicle_id or pri(c) <= pri(u):
                    continue
                cv = await ctx.vehicle_repo.get(c.assigned_vehicle_id)
                if cv is None or cv.status not in ("assigned", "accepted") or not vehicle_satisfies(cv.capability_tags, req):
                    continue
                dets.append(Detection(
                    "competing_incident", c.id, cv.id, "high" if pri(u) == 1 else "medium",
                    f"{cv.id} should stay on {c.id}",
                    [Evidence(key="competing_incident", value=u.id), Evidence(key="competing_priority", value=pri(u)),
                     Evidence(key="this_priority", value=pri(c)), Evidence(key="free_units", value=0)],
                    extra={"competing": u.id, "vehicle_id": cv.id}))
        return dets

    # ---- main loop ---------------------------------------------------------------------------------
    async def evaluate(self, actor: str = ACTOR) -> list[Risk]:
        ctx = self.ctx
        now = ctx.now()
        risks = await self.open_risks()
        open_by_key = {r.dedupe_key: r for r in risks}
        cooldown: dict[str, datetime] = ctx.extras.setdefault("risk_cooldown", {})
        seen: set[str] = set()
        created: list[Risk] = []
        for d in await self._detect(open_by_key):
            seen.add(d.key)
            ex = open_by_key.get(d.key)
            if ex is not None:  # dedupe: refresh evidence, no new alert
                ex.last_seen_at, ex.evidence, ex.seen_count = now, d.evidence, ex.seen_count + 1
                await self.repo.put(ex)
                continue
            until = cooldown.get(d.key)
            if until is not None and now < until:
                continue
            created.append(await self._open(d, actor))
        for r in risks:
            if r.dedupe_key not in seen and r.status in ("open", "awaiting"):
                r.status, r.resolution = "cleared", {"by": actor, "reason": "the condition no longer holds", "at": now.isoformat()}
                await self.repo.put(r)
                await ctx.audit.record(entity_type="risk", entity_id=r.id, event_type="risk.cleared", actor=actor,
                                       correlation_id=r.incident_id, source="simulated",
                                       reason="the condition no longer holds (hysteresis cleared)")
        return created

    async def _open(self, d: Detection, actor: str) -> Risk:
        ctx = self.ctx
        now = ctx.now()
        n = await ctx.store.next_seq("risk_seq")
        inc = await ctx.incident_repo.get(d.incident_id)
        risk = Risk(id=f"RSK-{n:04d}", incident_id=d.incident_id, type=d.type, severity=d.severity,  # type: ignore[arg-type]
                    entity=d.entity, dedupe_key=d.key, evidence=d.evidence, affected_assumption=d.assumption,
                    detected_at=now, last_seen_at=now)
        await self._build_options(risk, d, inc)
        await self.repo.put(risk)
        await ctx.audit.record(
            entity_type="risk", entity_id=risk.id, event_type="risk.detected", actor=actor, correlation_id=risk.incident_id,
            source="simulated", reason=d.assumption,
            payload={"type": d.type, "severity": d.severity, "entity": d.entity,
                     "evidence": {e.key: e.value for e in d.evidence},
                     "recommended": next((o.kind for o in risk.options if o.recommended), None),
                     "options": [o.kind for o in risk.options]})
        self._schedule_autopilot(risk)
        return risk

    # ---- options ------------------------------------------------------------------------------------
    def _opt(self, risk: Risk, kind: str, label: str, desc: str, effects: Effects | None = None, **kw) -> RecoveryOption:
        return RecoveryOption(id=f"{risk.id}-{kind}", kind=kind, label=label, description=desc,  # type: ignore[arg-type]
                              effects=effects or Effects(), **kw)

    async def _alternatives(self, inc: Incident) -> DispatchDecision | None:
        try:
            return await self.ctx.dispatch.recommend(inc.id, actor=ACTOR)
        except RuleViolation:
            return None

    def _plan_effects(self, cur: Candidate | None, alt: Candidate) -> tuple[Effects, float | None]:
        stale = any(r.startswith("STALE_HOSPITAL") for r in alt.reason_codes)
        eff = Effects(
            time_delta_s=None if cur is None else round(mid(alt) - mid(cur), 1),
            suitability="unverified" if stale else ("unchanged" if cur else "improved"),
            suitability_note="destination capacity data is unverified" if stale else "destination has fresh capacity data",
            coverage_delta=None if cur is None else round(cur.components.coverage_loss - alt.components.coverage_loss, 4),
            uncertainty="road estimates are ranges, not guarantees",
        )
        benefit = None if cur is None else round(cur.components.total - alt.components.total, 2)
        return eff, benefit

    async def _build_options(self, risk: Risk, d: Detection, inc: Incident | None) -> None:
        ctx, s = self.ctx, self.ctx.settings
        now = ctx.now()
        opts: list[RecoveryOption] = [self._opt(
            risk, "keep_current_plan", "Keep current plan",
            "Leave the assignment and destination as they are and keep monitoring.",
            Effects(time_delta_s=0, suitability="unchanged", suitability_note="the risk stays unresolved", coverage_delta=0),
            trade_offs=[f"Assumption stays unverified: {d.assumption}"], requires_approval=False)]
        dec: DispatchDecision | None = None
        t = d.type
        if inc is not None and t in ("stale_vehicle_gps", "no_acknowledgment", "capacity_change", "capability_change",
                                     "stale_hospital_resource", "eta_drift", "route_closure", "state_conflict"):
            dec = await self._alternatives(inc)
        cands = dec.candidates if dec else []
        cur = next((c for c in cands if c.is_current), None)
        cur_v = inc.assigned_vehicle_id if inc else None
        alt_unit = next((c for c in cands if c.vehicle_id != cur_v), None)
        alt_dest = next((c for c in cands if c.vehicle_id == cur_v and (cur is None or c.hospital_id != cur.hospital_id)), None)

        if t in CONFIRM_FIRST or t == "no_acknowledgment":
            target = "hospital" if t == "stale_hospital_resource" else "vehicle"
            msg = (f"Ask {d.entity} to confirm {next((e.value for e in d.evidence if e.key == 'resource'), 'resources')} now"
                   if target == "hospital" else f"Ask crew of {d.entity} to confirm position and status")
            opts.append(self._opt(
                risk, "request_confirmation", "Request confirmation", msg + " (simulated message; no real contact).",
                Effects(time_delta_s=0, suitability="unverified", suitability_note="cheap and reversible; resolves the unknown",
                        coverage_delta=0, uncertainty="clears itself when fresh data arrives"),
                trade_offs=["Takes a moment; the unit keeps moving on the current plan meanwhile"],
                payload={"target_kind": target, "target_id": d.entity,
                         "resource": next((e.value for e in d.evidence if e.key == "resource"), None)}))
        if t == "route_closure" or t == "eta_drift":
            opts.append(self._opt(
                risk, "reroute", "Reroute", "Offer the driver a fresh route; it applies only when the driver taps.",
                Effects(suitability="unchanged", suitability_note="same destination", coverage_delta=0,
                        uncertainty="new route is also an estimate"),
                trade_offs=["Driver must accept; a new route can be longer than the blocked one"],
                changes_plan=True, necessary=t == "route_closure", payload={"vehicle_id": d.entity, "reason": t}))
        if alt_dest is not None and t in ("capacity_change", "capability_change", "stale_hospital_resource", "eta_drift", "no_acknowledgment"):
            eff, ben = self._plan_effects(cur, alt_dest)
            opts.append(self._opt(
                risk, "alternate_destination", f"Alternate destination: {alt_dest.hospital_id}",
                f"Send the same unit to {alt_dest.hospital_id}. The first hospital's pre-alert is marked redirected, not deleted.",
                eff, trade_offs=["Receiving hospital must be pre-alerted again", "ETA range changes"],
                benefit=ben, changes_plan=True, necessary=t in ("capacity_change", "capability_change"),
                payload={"vehicle_id": alt_dest.vehicle_id, "hospital_id": alt_dest.hospital_id, "decision_id": dec.id if dec else None}))
        if alt_unit is not None and t in ("stale_vehicle_gps", "no_acknowledgment", "eta_drift", "state_conflict", "route_closure"):
            eff, ben = self._plan_effects(cur, alt_unit)
            eff.other_incidents = []
            opts.append(self._opt(
                risk, "propose_eligible_unit", f"Propose unit {alt_unit.vehicle_id}",
                f"Assign {alt_unit.vehicle_id} (eligible, fresh position) and release {cur_v}. {cur_v} is not cancelled silently: the release is audited.",
                eff, trade_offs=[f"{cur_v} stops being used for this incident", "Needs the new crew to accept"],
                benefit=ben, changes_plan=True, necessary=t == "no_acknowledgment",
                payload={"vehicle_id": alt_unit.vehicle_id, "hospital_id": alt_unit.hospital_id, "decision_id": dec.id if dec else None}))
        if t == "competing_incident":
            comp = d.extra["competing"]
            gap = next(e.value for e in d.evidence if e.key == "this_priority") - next(e.value for e in d.evidence if e.key == "competing_priority")
            cinc = await ctx.incident_repo.get(comp)
            opts.append(self._opt(
                risk, "propose_eligible_unit", f"Hand {d.entity} to {comp}",
                f"Release {d.entity} from {d.incident_id} and re-dispatch both incidents; {comp} is more urgent.",
                Effects(time_delta_s=None, suitability="improved", suitability_note=f"{comp} gets a suitable unit",
                        coverage_delta=0, other_incidents=[{"incident_id": d.incident_id,
                                                            "impact": "loses its unit; re-ranked as soon as one is free (ETA unknown)"}],
                        uncertainty="no replacement unit is available right now"),
                trade_offs=[f"{d.incident_id} waits longer", "Both incidents are re-dispatched and need approval again"],
                benefit=float(gap) * 2, changes_plan=True,
                payload={"competing": comp, "vehicle_id": d.entity, "competing_priority": pri(cinc) if cinc else None}))
        opts.append(self._opt(
            risk, "escalate", "Escalate", "Tell a supervisor and place a simulated voice call to the crew or hospital. Nothing changes automatically.",
            Effects(time_delta_s=None, suitability="unverified", suitability_note="a human takes over", coverage_delta=0),
            trade_offs=["Slowest option; uses supervisor attention"], payload={"simulated": True}))

        for o in opts:
            if o.changes_plan:
                o.below_threshold = (not o.necessary) and (o.benefit is None or o.benefit < s.min_reassign_benefit)
        best = None
        changers = [o for o in opts if o.changes_plan and o.feasible and not o.below_threshold]
        if changers:
            best = max(changers, key=lambda o: (o.necessary, o.benefit if o.benefit is not None else 99))
        conf = next((o for o in opts if o.kind == "request_confirmation"), None)
        if t in CONFIRM_FIRST and conf is not None and not (best and best.necessary):
            best = conf
        if t == "no_acknowledgment" and best is None and conf is not None:
            best = conf
        for o in opts:
            o.recommended = o is best
        if best is None:
            opts[0].recommended = True
        risk.options, risk.options_at = opts, now
        risk.decision_id = dec.id if dec else None

    # ---- applying an option -----------------------------------------------------------------------------
    async def recover(self, risk_id: str, kind: str, reason: str, actor: str) -> Risk:
        ctx = self.ctx
        risk = await self.get(risk_id)
        if risk.status not in ("open", "awaiting"):
            raise RuleViolation(f"risk is {risk.status}")
        opt = next((o for o in risk.options if o.kind == kind), None)
        if opt is None:
            raise RuleViolation(f"option '{kind}' does not exist for this risk")
        if not opt.feasible:
            raise RuleViolation("option is not feasible")
        now = ctx.now()
        inc = await ctx.incident_repo.get(risk.incident_id)
        outcome: dict[str, Any] = {"option": kind, "by": actor, "reason": reason, "at": now.isoformat()}
        new_status = "resolved"

        if kind == "keep_current_plan":
            pass
        elif kind == "escalate":
            risk.escalated = True
            new_status = "open"
            outcome["message"] = "Supervisor notified and simulated voice call placed. No real call was made."
        elif kind == "request_confirmation":
            n = await ctx.store.next_seq("confirm_seq")
            p = opt.payload
            c = ConfirmationRequest(id=f"CNF-{n:04d}", target_kind=p["target_kind"], target_id=p["target_id"],
                                    resource=p.get("resource"), requested_at=now, requested_by=actor,
                                    message=opt.description)
            risk.confirmations.append(c)
            new_status = "awaiting"
            outcome["confirmation_id"] = c.id
            vis = {"vehicle_id": c.target_id} if c.target_kind == "vehicle" else {"hospital_id": c.target_id}
            ctx.hub.publish("risk.confirmation_requested", c.target_id, correlation_id=risk.incident_id, source="simulated",
                            payload={"risk_id": risk.id, "message": c.message, "resource": c.resource}, vis=vis)
        elif kind == "reroute":
            prop = await ctx.nav.force_proposal(opt.payload["vehicle_id"], reason, actor)
            if prop is None:
                raise RuleViolation("vehicle has no active route to reroute")
            new_status = "awaiting"
            outcome["proposal_id"] = prop.id
        elif kind in ("alternate_destination", "propose_eligible_unit"):
            if risk.type == "competing_incident":
                outcome |= await self._reassign_for_competing(risk, opt, actor, reason)
            else:
                dec = await ctx.dispatch.repo.get(opt.payload.get("decision_id") or "")
                if dec is None or dec.status != "pending":
                    await self.refresh_options(risk.id)
                    raise RuleViolation("the options were out of date and have been recalculated; review them and approve again")
                body = ApproveRequest(action="override", reason=reason, vehicle_id=opt.payload["vehicle_id"],
                                      hospital_id=opt.payload["hospital_id"])
                done = await ctx.dispatch.approve(dec.id, body, actor)
                outcome["decision_id"] = done.id
                outcome["final"] = done.final
        risk.status = new_status  # type: ignore[assignment]
        risk.resolution = outcome
        await self.repo.put(risk)
        ctx.extras.setdefault("risk_cooldown", {})[risk.dedupe_key] = now + timedelta(seconds=ctx.settings.risk_cooldown_s)
        await ctx.audit.record(
            entity_type="risk", entity_id=risk.id, event_type="risk.recovery_approved", actor=actor,
            correlation_id=risk.incident_id, source="human_confirmed" if actor != AUTOPILOT else "simulated", reason=reason,
            old="open", new=new_status, payload={"option": kind, "autopilot": actor == AUTOPILOT, **{k: v for k, v in outcome.items() if k not in ("reason", "by", "at")}},
            vis=vis_for(inc) if inc else None)
        return risk

    async def _reassign_for_competing(self, risk: Risk, opt: RecoveryOption, actor: str, reason: str) -> dict[str, Any]:
        ctx = self.ctx
        vid, comp = opt.payload["vehicle_id"], opt.payload["competing"]
        v = await ctx.vehicle_repo.get(vid)
        cinc = await ctx.incident_repo.get(risk.incident_id)
        if v is None or cinc is None or v.status not in ("assigned", "accepted"):
            raise RuleViolation("unit has already moved on; nothing was changed")
        now = ctx.now()
        await ctx.vehicles.apply_update(vid, VehicleStateUpdate(
            update_id=f"{risk.id}-release", occurred_at=now, status="available",
            reason=f"released for competing incident {comp}: {reason}", source="reported"), actor)
        await ctx.acks.supersede(vid, cinc.id, actor, f"competing incident {comp}")
        ctx.nav.reset(vid)
        ctx.demo_driver.cancel(vid)

        def clear(i: Incident) -> Incident:
            i.assigned_vehicle_id, i.status, i.updated_at = None, "new", now
            return i

        cinc, _ = await ctx.incident_repo.modify(cinc.id, clear)
        await ctx.audit.record(
            entity_type="incident", entity_id=cinc.id, event_type="incident.unit_released_for_competing", actor=actor,
            correlation_id=cinc.id, source="human_confirmed", reason=reason, old={"vehicle": vid}, new={"vehicle": None},
            payload={"competing_incident": comp, "risk_id": risk.id}, vis=vis_for(cinc))
        ctx.pipeline.start(comp)
        ctx.pipeline.start(cinc.id)
        return {"released_vehicle": vid, "redispatching": [comp, cinc.id]}

    async def refresh_options(self, risk_id: str) -> Risk:
        risk = await self.get(risk_id)
        inc = await ctx_get(self.ctx, risk.incident_id)
        d = Detection(risk.type, risk.incident_id, risk.entity, risk.severity, risk.affected_assumption, risk.evidence,
                      extra=({"competing": next((e.value for e in risk.evidence if e.key == "competing_incident"), None),
                              "vehicle_id": risk.entity} if risk.type == "competing_incident" else {}))
        await self._build_options(risk, d, inc)
        await self.repo.put(risk)
        return risk

    # ---- Demo Autopilot --------------------------------------------------------------------------------------
    def _schedule_autopilot(self, risk: Risk) -> None:
        ctx = self.ctx
        rec = next((o for o in risk.options if o.recommended), None)
        if not ctx.autopilot.enabled or rec is None or rec.kind in ("keep_current_plan", "escalate"):
            return  # the autopilot only approves actions; keeping the plan or escalating is left to a human
        ctx.spawn(self._autopilot_apply(risk.id, rec.kind))

    async def _autopilot_apply(self, risk_id: str, kind: str) -> None:
        delay = self.ctx.settings.risk_autopilot_delay_s
        if delay > 0:
            await asyncio.sleep(delay)
        if not self.ctx.autopilot.enabled:
            return
        r = await self.repo.get(risk_id)
        if r is None or r.status != "open":
            return
        try:
            await self.recover(risk_id, kind, f"Demo Autopilot: simulated dispatcher approval of the recommended option after {delay:g}s", AUTOPILOT)
        except RuleViolation as e:
            await self.ctx.audit.record(entity_type="risk", entity_id=risk_id, event_type="risk.autopilot_skipped",
                                        actor=AUTOPILOT, correlation_id=r.incident_id, reason=str(e), source="simulated")


async def ctx_get(ctx, incident_id: str) -> Incident | None:
    return await ctx.incident_repo.get(incident_id)

