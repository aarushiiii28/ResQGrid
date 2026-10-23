"""M4 dispatch engine. P0 = nearest eligible baseline. P1 = travel-time-aware ranking of
vehicle/destination pairs under hard constraints with a configurable weighted score.
Recommendations never commit anything; approval is a separate, audited human action."""

import asyncio
from datetime import datetime
from typing import Any

from app.core.capabilities import CAP_RESOURCE, CATEGORY_HOSPITAL_CAPS, CATEGORY_VEHICLE_TAGS, vehicle_satisfies
from app.core.geo import haversine_m
from app.db.repos import Repo
from app.optimization.baseline import nearest_capable_hospital, nearest_vehicle
from app.optimization.coverage import coverage_fraction, make_grid
from app.optimization.ortools_solver import greedy, ortools_assign
from app.optimization.scoring import PairInputs, score_pair, weights_from
from app.schemas.dispatch import (
    ApproveRequest,
    Candidate,
    DispatchDecision,
    Exclusion,
    Weights,
    WeightsOverride,
)
from app.schemas.hospital import Hospital, HospitalView
from app.schemas.incident import Incident
from app.schemas.routing import EntranceDecision, RouteEstimate
from app.schemas.vehicle import Vehicle, VehicleStateUpdate
from app.services.hospital_service import make_view as hospital_view
from app.services.incident_service import NOT_DISPATCHABLE, RuleViolation, vis_for
from app.services.vehicle_service import make_view as vehicle_view

POLICY_VERSION = {"P0": "P0-nearest-eligible-v1", "P1": "P1-ranked-pairs-v1"}
ASSUMPTIONS = [
    "Travel times come from the configured routing provider (mock = deterministic street grid).",
    "Coverage loss uses a uniform demand grid and a straight-line response radius; a prototype approximation.",
    "Capability and resource rules are configuration, not clinical guidance.",
    "Hospital resources that are stale or never confirmed count as unknown, not available.",
    "Scores are in minute-equivalents and are not clinically validated.",
]
LatLon = tuple[float, float]


def category_of(inc: Incident) -> str:
    c = inc.reported_category.value if inc.reported_category else "unknown"
    return c if c in CATEGORY_VEHICLE_TAGS else "unknown"


def rec_eta(dec: DispatchDecision, vid: str, hid: str) -> tuple[float, float] | None:
    for c in dec.candidates:
        if c.vehicle_id == vid and c.hospital_id == hid and c.eta_total_low_s is not None:
            return c.eta_total_low_s, c.eta_total_high_s
    return None


class DispatchService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[DispatchDecision] = Repo(ctx.store, "decision", DispatchDecision)
        self._grid: list[LatLon] | None = None

    @property
    def grid(self) -> list[LatLon]:
        if self._grid is None:
            s = self.ctx.settings
            self._grid = make_grid(self.ctx.fleet_centre or (s.city_lat, s.city_lng))
        return self._grid

    # ---- helpers -----------------------------------------------------
    async def scene_point(self, inc: Incident, disruptions) -> tuple[LatLon, EntranceDecision | None, bool]:
        """Scene = chosen entrance node when the incident has a building, else the best location."""
        if not inc.location_candidates:
            raise RuleViolation("incident has no location; add a location or an approximate pin first")
        loc = inc.location_candidates[0]
        approx = loc.approximate or (loc.accuracy_m or 0) > 100
        if inc.building_id:
            b = await self.ctx.building_repo.get(inc.building_id)
            if b:
                dec = self.ctx.access.decide(b, disruptions.blocked_entrances, (loc.lat, loc.lon))
                if dec.chosen:
                    e = dec.chosen.entrance.location
                    return (e.lat, e.lon), dec, approx
                return (b.centroid.lat, b.centroid.lon), dec, approx
        return (loc.lat, loc.lon), None, approx

    @staticmethod
    def hospital_status(view: HospitalView, caps: set[str], stale_s: float) -> tuple[bool, float, list[str], dict[str, bool]]:
        """(feasible, freshness 0..1, reason codes, constraints) for the resources the caps need."""
        reasons, fresh, feasible = [], 0.0, True
        for cap in sorted(caps):
            rname = CAP_RESOURCE.get(cap)
            rv = view.resources.get(rname) if rname else None
            if rv is None or rv.status == "unavailable":
                feasible = False
                reasons.append(f"RESOURCE_UNAVAILABLE:{rname}")
            elif rv.status == "unknown":
                fresh = max(fresh, 1.0)
                reasons.append(f"STALE_HOSPITAL_DATA:{rname}")
            else:
                fresh = max(fresh, min(1.0, (rv.age_s or 0) / stale_s))
        if feasible and not reasons:
            reasons.append("FRESH_HOSPITAL_DATA")
        return feasible, fresh, reasons, {"resource_not_unavailable": feasible}

    async def _route(self, a: LatLon, b: LatLon, disruptions, memo: dict) -> RouteEstimate:
        key = (round(a[0], 6), round(a[1], 6), round(b[0], 6), round(b[1], 6))
        if key not in memo:
            memo[key] = await self.ctx.routing.route(a, b, disruptions)
        return memo[key]

    # ---- evaluation core ---------------------------------------------
    async def evaluate(self, inc: Incident, *, weights: Weights, k_vehicles: int | None = None) -> dict[str, Any]:
        ctx, s = self.ctx, self.ctx.settings
        now = ctx.now()
        disruptions = await ctx.routing.active_disruptions()
        scene, entrance, approx = await self.scene_point(inc, disruptions)
        cat = category_of(inc)
        req_tags, req_caps = CATEGORY_VEHICLE_TAGS[cat], CATEGORY_HOSPITAL_CAPS[cat]

        elig = await ctx.vehicles.eligible(req_tags)
        exclusions: list[Exclusion] = [Exclusion(kind="vehicle", id=v.id, reasons=why) for v, ok, why in elig if not ok]
        pool = [v for v, ok, _ in elig if ok]
        current_v = None
        if inc.assigned_vehicle_id:
            cv = await ctx.vehicle_repo.get(inc.assigned_vehicle_id)
            if cv and cv.status in ("assigned", "accepted", "en_route") and cv.location and vehicle_satisfies(cv.capability_tags, req_tags):
                current_v = cv
                if cv.id not in {v.id for v in pool}:
                    pool.append(cv)
                    exclusions = [e for e in exclusions if e.id != cv.id]

        def dist(v: Vehicle) -> float:
            return haversine_m(scene[0], scene[1], v.location.lat, v.location.lon)

        pool.sort(key=lambda v: (dist(v), v.id))
        k = k_vehicles or s.candidate_vehicles
        cand_vehicles = pool[:k]
        if current_v and current_v.id not in {v.id for v in cand_vehicles}:
            cand_vehicles.append(current_v)

        hospitals = await ctx.hospital_repo.list()
        hviews = {h.id: hospital_view(h, now, s.hospital_stale_s) for h in hospitals}
        feasible_h: list[tuple[Hospital, float, list[str], dict[str, bool]]] = []
        for h in hospitals:
            if not req_caps <= set(h.capabilities):
                exclusions.append(Exclusion(kind="hospital", id=h.id, reasons=[
                    f"missing_capability:{c}" for c in sorted(req_caps - set(h.capabilities))]))
                continue
            ok, fr, why, cons = self.hospital_status(hviews[h.id], req_caps, s.hospital_stale_s)
            if not ok:
                exclusions.append(Exclusion(kind="hospital", id=h.id, reasons=why))
                continue
            feasible_h.append((h, fr, why, cons))
        feasible_h.sort(key=lambda t: (haversine_m(scene[0], scene[1], t[0].location.lat, t[0].location.lon), t[0].id))
        top_h = feasible_h[: s.candidate_hospitals]
        if inc.destination_id and inc.destination_id not in {t[0].id for t in top_h}:
            top_h += [t for t in feasible_h if t[0].id == inc.destination_id]

        memo: dict = {}
        v_routes = await asyncio.gather(*[
            self._route((v.location.lat, v.location.lon), scene, disruptions, memo) for v in cand_vehicles])
        h_routes = await asyncio.gather(*[
            self._route(scene, (h.location.lat, h.location.lon), disruptions, memo) for h, *_ in top_h])

        units = {v.id: (v.location.lat, v.location.lon) for v in pool if v.status == "available"}
        cov_before = coverage_fraction(list(units.values()), self.grid, s.coverage_radius_m)
        cov_loss = {}
        for v in cand_vehicles:
            if v.id in units:
                after = coverage_fraction([p for i, p in units.items() if i != v.id], self.grid, s.coverage_radius_m)
                cov_loss[v.id] = max(0.0, cov_before - after)
            else:
                cov_loss[v.id] = 0.0

        nearest_id = pool[0].id if pool else None
        has_plan = bool(inc.assigned_vehicle_id)
        cands: list[Candidate] = []
        for v, vr in zip(cand_vehicles, v_routes, strict=True):
            vview = vehicle_view(v, now, s.vehicle_stale_s)
            vfresh = min(1.0, (vview.location_age_s or 0) / s.vehicle_stale_s)
            for (h, hfresh, hwhy, cons), hr in zip(top_h, h_routes, strict=True):
                sw = 0.0
                if has_plan:
                    sw = (1.0 if v.id != inc.assigned_vehicle_id else 0.0) + (
                        0.5 if inc.destination_id and h.id != inc.destination_id else 0.0)
                comp = score_pair(PairInputs(
                    scene_mid_s=vr.duration_s, hospital_mid_s=hr.duration_s,
                    total_low_s=vr.duration_low_s + hr.duration_low_s,
                    total_high_s=vr.duration_high_s + hr.duration_high_s,
                    vehicle_freshness=vfresh, hospital_freshness=hfresh, coverage_loss=cov_loss[v.id],
                    entrance_uncertain=bool(entrance and entrance.needs_confirmation),
                    degraded_routing=vr.degraded or hr.degraded, approximate_location=approx, switch=sw,
                ), weights)
                codes = ["ELIGIBLE", "CAPABLE_DESTINATION", *hwhy]
                codes.append("STALE_VEHICLE_LOCATION" if vfresh >= 1.0 else "FRESH_VEHICLE_LOCATION")
                if v.id == nearest_id:
                    codes.append("NEAREST_UNIT")
                codes.append("LOW_COVERAGE_IMPACT" if cov_loss[v.id] < 0.02 else
                             "HIGH_COVERAGE_IMPACT" if cov_loss[v.id] > 0.08 else "MODERATE_COVERAGE_IMPACT")
                if entrance and entrance.needs_confirmation:
                    codes.append("ENTRANCE_UNCERTAIN")
                if vr.degraded or hr.degraded:
                    codes.append("DEGRADED_ROUTING")
                if sw:
                    codes.append("SWITCH_PENALTY_APPLIED")
                if vr.applied_disruptions or hr.applied_disruptions:
                    codes.append("DISRUPTION_APPLIED")
                cands.append(Candidate(
                    vehicle_id=v.id, hospital_id=h.id, feasible=True,
                    is_current=bool(has_plan and v.id == inc.assigned_vehicle_id and h.id == inc.destination_id),
                    eta_scene_low_s=vr.duration_low_s, eta_scene_s=vr.duration_s, eta_scene_high_s=vr.duration_high_s,
                    eta_hospital_s=hr.duration_s, eta_total_low_s=vr.duration_low_s + hr.duration_low_s,
                    eta_total_high_s=vr.duration_high_s + hr.duration_high_s, components=comp,
                    constraints={"available": True, "capability": True, **cons}, reason_codes=codes,
                ))
        cands.sort(key=lambda c: (c.components.total, c.vehicle_id, c.hospital_id))
        for i, c in enumerate(cands, 1):
            c.rank = i
        routes = {"vehicle": {v.id: r for v, r in zip(cand_vehicles, v_routes, strict=True)},
                  "hospital": {h.id: r for (h, *_), r in zip(top_h, h_routes, strict=True)}}
        return {
            "cands": cands, "exclusions": exclusions, "scene": scene, "entrance": entrance, "routes": routes,
            "category": cat, "req_tags": req_tags, "req_caps": req_caps, "disruptions": disruptions,
            "pool": pool, "hospitals": hospitals, "approx": approx, "now": now, "units": units,
            "snapshot": {
                "clock": now.isoformat(), "scene": list(scene), "category": cat,
                "required_vehicle_tags": sorted(req_tags), "required_hospital_caps": sorted(req_caps),
                "vehicles": {v.id: {"lat": v.location.lat, "lon": v.location.lon, "status": v.status,
                                    "location_age_s": vehicle_view(v, now, s.vehicle_stale_s).location_age_s}
                             for v in cand_vehicles},
                "hospitals": {h.id: {k: {"status": r.status, "age_s": r.age_s} for k, r in hviews[h.id].resources.items()}
                              for h, *_ in top_h},
                "disruptions": [d.id for d in disruptions.items],
                "current": {"vehicle": inc.assigned_vehicle_id, "destination": inc.destination_id},
            },
        }

    async def _baseline(self, inc: Incident, ev: dict, weights: Weights) -> Candidate | None:
        """P0: nearest eligible vehicle (straight line) and nearest hospital with the capability tag."""
        v = nearest_vehicle([x for x in ev["pool"] if x.status == "available"], ev["scene"])
        h = nearest_capable_hospital(ev["hospitals"], ev["scene"], ev["req_caps"])
        if v is None or h is None:
            return None
        s = self.ctx.settings
        hv = hospital_view(h, ev["now"], s.hospital_stale_s)
        ok, hfresh, hwhy, cons = self.hospital_status(hv, ev["req_caps"], s.hospital_stale_s)
        vr = ev["routes"]["vehicle"].get(v.id) or await self.ctx.routing.route(
            (v.location.lat, v.location.lon), ev["scene"], ev["disruptions"])
        hr = ev["routes"]["hospital"].get(h.id) or await self.ctx.routing.route(
            ev["scene"], (h.location.lat, h.location.lon), ev["disruptions"])
        vview = vehicle_view(v, ev["now"], s.vehicle_stale_s)
        comp = score_pair(PairInputs(
            scene_mid_s=vr.duration_s, hospital_mid_s=hr.duration_s,
            total_low_s=vr.duration_low_s + hr.duration_low_s, total_high_s=vr.duration_high_s + hr.duration_high_s,
            vehicle_freshness=min(1.0, (vview.location_age_s or 0) / s.vehicle_stale_s), hospital_freshness=hfresh,
            coverage_loss=0.0,
        ), weights)
        codes = ["BASELINE_NEAREST_ELIGIBLE", "BASELINE_IGNORES_FRESHNESS_AND_COVERAGE", *hwhy]
        return Candidate(
            vehicle_id=v.id, hospital_id=h.id, feasible=ok, eta_scene_low_s=vr.duration_low_s,
            eta_scene_s=vr.duration_s, eta_scene_high_s=vr.duration_high_s, eta_hospital_s=hr.duration_s,
            eta_total_low_s=vr.duration_low_s + hr.duration_low_s,
            eta_total_high_s=vr.duration_high_s + hr.duration_high_s, components=comp,
            constraints={"available": True, "capability": True, **cons}, reason_codes=codes,
        )

    # ---- public API ----------------------------------------------------
    async def recommend(self, incident_id: str, *, policy: str = "P1", override: WeightsOverride | None = None,
                        actor: str = "system") -> DispatchDecision:
        ctx = self.ctx
        inc = await ctx.incidents.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if inc.status in NOT_DISPATCHABLE:
            raise RuleViolation(f"incident is {inc.status}")
        w = weights_from(ctx.settings, override)
        ev = await self.evaluate(inc, weights=w)
        baseline = await self._baseline(inc, ev, w)
        cands: list[Candidate] = ev["cands"]
        keep_current, rec = False, None
        codes_extra: list[str] = []
        if policy == "P0":
            rec = baseline
            cands = [baseline] if baseline else []
        else:
            rec = cands[0] if cands else None
            cur = next((c for c in cands if c.is_current), None)
            if rec and cur and rec is not cur:
                if cur.components.total - rec.components.total < ctx.settings.min_reassign_benefit:
                    rec, keep_current = cur, True
                    codes_extra.append("KEEP_CURRENT_BENEFIT_BELOW_THRESHOLD")
                else:
                    codes_extra.append("REASSIGN_BENEFIT_ABOVE_THRESHOLD")
            elif inc.assigned_vehicle_id and cur is None and rec:
                codes_extra.append("CURRENT_PLAN_NO_LONGER_FEASIBLE")
            if rec is not None and codes_extra:
                rec = rec.model_copy(update={"reason_codes": [*rec.reason_codes, *codes_extra]})
        if rec is None:
            raise RuleViolation(
                "no feasible vehicle/destination pair; see exclusions. Dispatcher must resolve manually.")

        # supersede older pending decisions for the same incident
        for old in await self.repo.list():
            if old.incident_id == incident_id and old.status == "pending":
                old.status = "superseded"
                await self.repo.put(old)
        n = await ctx.store.next_seq("decision_seq")
        dec = DispatchDecision(
            id=f"DEC-{n:04d}", incident_id=incident_id, policy=policy, policy_version=POLICY_VERSION[policy],  # type: ignore[arg-type]
            created_at=ev["now"], weights=w, candidates=cands, exclusions=ev["exclusions"], recommendation=rec,
            baseline=baseline, keep_current=keep_current, assumptions=ASSUMPTIONS, inputs=ev["snapshot"],
            scene_route=ev["routes"]["vehicle"].get(rec.vehicle_id),
            hospital_route=ev["routes"]["hospital"].get(rec.hospital_id), entrance=ev["entrance"],
        )
        await self.repo.put(dec)
        await ctx.audit.record(
            entity_type="dispatch", entity_id=dec.id, event_type="dispatch.recommended", actor=actor,
            correlation_id=incident_id, source="simulated",
            payload={"policy": policy, "vehicle_id": rec.vehicle_id, "hospital_id": rec.hospital_id,
                     "score": rec.components.total if rec.components else None, "keep_current": keep_current,
                     "baseline": baseline.vehicle_id if baseline else None},
        )
        return dec

    async def approve(self, decision_id: str, body: ApproveRequest, actor: str) -> DispatchDecision:
        ctx = self.ctx
        dec = await self.repo.get(decision_id)
        if dec is None:
            raise KeyError(decision_id)
        if dec.status != "pending":
            raise RuleViolation(f"decision is {dec.status}, not pending")
        inc = await ctx.incidents.get(dec.incident_id)
        now = ctx.now()
        if body.action == "reject":
            dec.status, dec.approved_by, dec.decided_at, dec.rationale = "rejected", actor, now, body.reason
            await self.repo.put(dec)
            await ctx.audit.record(entity_type="dispatch", entity_id=dec.id, event_type="dispatch.rejected",
                                   actor=actor, correlation_id=dec.incident_id, reason=body.reason, source="human_confirmed")
            await ctx.pipeline.on_decision(dec, inc, None, actor)
            return dec
        rec = dec.recommendation
        vid, hid = (rec.vehicle_id, rec.hospital_id) if rec else (None, None)
        overridden = False
        if body.action == "override":
            if not body.vehicle_id and not body.hospital_id:
                raise RuleViolation("override needs vehicle_id and/or hospital_id")
            vid, hid = body.vehicle_id or vid, body.hospital_id or hid
            overridden = True
        if body.action == "approve" and dec.keep_current:
            dec.status, dec.approved_by, dec.decided_at, dec.rationale = "kept_current", actor, now, body.reason
            await self.repo.put(dec)
            await ctx.audit.record(entity_type="dispatch", entity_id=dec.id, event_type="dispatch.kept_current",
                                   actor=actor, correlation_id=dec.incident_id, reason=body.reason, source="human_confirmed")
            await ctx.pipeline.on_decision(dec, inc, None, actor)
            return dec

        # hard constraints are re-checked at approval time and cannot be overridden
        await self._check_hard(inc, vid, hid)
        old_v, old_h = inc.assigned_vehicle_id, inc.destination_id
        if old_v and old_v != vid:
            ov = await ctx.vehicle_repo.get(old_v)
            if ov and ov.status in ("on_scene", "transporting"):
                raise RuleViolation("cannot reassign a unit that is already on scene or transporting")
            if ov and ov.status != "available":
                await ctx.vehicles.apply_update(old_v, VehicleStateUpdate(
                    update_id=f"{dec.id}-release-{old_v}", occurred_at=now, status="available",
                    reason=f"released by reassignment ({body.reason})", source="reported"), actor)
                await ctx.acks.supersede(old_v, inc.id, actor, body.reason)
            ctx.nav.reset(old_v)
            ctx.demo_driver.cancel(old_v)
        assignment_id = None
        if vid != old_v:
            await ctx.vehicles.apply_update(vid, VehicleStateUpdate(
                update_id=f"{dec.id}-assign-{vid}", occurred_at=now, status="assigned", incident_id=inc.id,
                reason=body.reason, source="reported"), actor)
        inc = await ctx.incidents.assign(inc.id, vid, hid, actor, body.reason)
        if vid != old_v:
            a = await ctx.acks.create_and_send(inc, vid, hid, dec, actor)
            assignment_id = a.id
        assignment = None
        if assignment_id:
            assignment = await ctx.acks.repo.get(assignment_id)
        dec.status = "overridden" if overridden else "approved"
        dec.approved_by, dec.decided_at, dec.rationale = actor, now, body.reason
        dec.final = {"vehicle_id": vid, "hospital_id": hid, "previous": {"vehicle_id": old_v, "hospital_id": old_h}}
        dec.assignment_id = assignment_id
        await self.repo.put(dec)
        await ctx.audit.record(
            entity_type="dispatch", entity_id=dec.id,
            event_type="dispatch.overridden" if overridden else "dispatch.approved", actor=actor,
            correlation_id=inc.id, reason=body.reason, source="human_confirmed" if actor != "demo-autopilot" else "simulated",
            old={"vehicle_id": old_v, "hospital_id": old_h}, new={"vehicle_id": vid, "hospital_id": hid},
            payload={"assignment_id": assignment_id, "autopilot": actor == "demo-autopilot"}, vis=vis_for(inc),
        )
        await ctx.cases.upsert_for_incident(inc, vid, hid, rec_eta(dec, vid, hid), actor)
        await ctx.pipeline.on_decision(dec, inc, assignment, actor)
        return dec

    async def _check_hard(self, inc: Incident, vid: str | None, hid: str | None) -> None:
        ctx, s = self.ctx, self.ctx.settings
        cat = category_of(inc)
        v = await ctx.vehicle_repo.get(vid) if vid else None
        h = await ctx.hospital_repo.get(hid) if hid else None
        if v is None or h is None:
            raise RuleViolation("unknown vehicle or hospital")
        if v.id != inc.assigned_vehicle_id:
            from app.services.vehicle_service import eligibility

            ok, why = eligibility(v, CATEGORY_VEHICLE_TAGS[cat], ctx.now(), s.vehicle_stale_s)
            if not ok:
                raise RuleViolation(f"vehicle {v.id} violates hard constraints: {', '.join(why)}. Re-run recommend.")
        caps = CATEGORY_HOSPITAL_CAPS[cat]
        if not caps <= set(h.capabilities):
            raise RuleViolation(f"hospital {h.id} lacks required capability: {', '.join(sorted(caps - set(h.capabilities)))}")
        ok, _, why, _ = self.hospital_status(hospital_view(h, ctx.now(), s.hospital_stale_s), caps, s.hospital_stale_s)
        if not ok:
            raise RuleViolation(f"hospital {h.id} resource unavailable: {', '.join(why)}")

    async def hospital_match(self, category: str, scene: LatLon) -> list[dict]:
        """Nearest capable hospital with fresh capacity data first; stale/unknown ranked after."""
        ctx, s = self.ctx, self.ctx.settings
        cat = category if category in CATEGORY_HOSPITAL_CAPS else "other"
        caps = CATEGORY_HOSPITAL_CAPS[cat]
        disruptions = await ctx.routing.active_disruptions()
        rows = []
        for h in await ctx.hospital_repo.list():
            view = hospital_view(h, ctx.now(), s.hospital_stale_s)
            if not caps <= set(h.capabilities):
                rows.append({"hospital_id": h.id, "feasible": False, "tier": 3, "reasons": [
                    f"missing_capability:{c}" for c in sorted(caps - set(h.capabilities))]})
                continue
            ok, fr, why, _ = self.hospital_status(view, caps, s.hospital_stale_s)
            r = await ctx.routing.route(scene, (h.location.lat, h.location.lon), disruptions)
            tier = 3 if not ok else (2 if fr >= 1.0 else 1)
            rows.append({"hospital_id": h.id, "feasible": ok, "tier": tier, "freshness": round(fr, 3),
                         "eta_low_s": r.duration_low_s, "eta_s": r.duration_s, "eta_high_s": r.duration_high_s,
                         "route_source": r.source, "reasons": why})
        rows.sort(key=lambda r: (r["tier"], r.get("eta_s", 1e12), r["hospital_id"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    async def batch_compare(self, incident_ids: list[str]) -> dict:
        """Multi-incident assignment: greedy (priority order) versus OR-Tools CP-SAT on the same costs."""
        ctx = self.ctx
        w = weights_from(ctx.settings)
        incs = []
        for i in incident_ids:
            inc = await ctx.incidents.get(i)
            if inc is None:
                raise KeyError(i)
            incs.append(inc)
        incs.sort(key=lambda x: (x.operator_priority.value if x.operator_priority else 9, x.created_at, x.id))
        cost: dict[tuple[str, str], float] = {}
        best_pair: dict[tuple[str, str], Candidate] = {}
        for inc in incs:
            ev = await self.evaluate(inc, weights=w, k_vehicles=ctx.settings.batch_candidate_vehicles)
            for c in ev["cands"]:
                key = (inc.id, c.vehicle_id)
                if key not in cost or c.components.total < cost[key]:
                    cost[key] = c.components.total
                    best_pair[key] = c
        order = [i.id for i in incs]
        g = greedy(order, cost)
        o = ortools_assign(order, cost)

        def describe(sol):
            return {
                "solver": sol.solver, "status": sol.status, "total_cost": sol.total_cost,
                "assigned": len(sol.assignment), "unassigned": sol.unassigned,
                "assignments": {i: {"vehicle_id": v, "hospital_id": best_pair[(i, v)].hospital_id,
                                    "cost": cost[(i, v)]} for i, v in sol.assignment.items()},
            }

        return {
            "policy_note": "Costs are P1 scores (best destination per vehicle). Hospital capacity is not shared "
                           "across incidents here; mass-casualty splitting is handled separately.",
            "incident_order": order, "greedy": describe(g), "ortools": describe(o),
            "improvement": round(g.total_cost - o.total_cost, 4) if len(g.assignment) == len(o.assignment) else None,
            "more_incidents_served": len(o.assignment) - len(g.assignment),
        }

    @staticmethod
    def age(now: datetime, t: datetime | None) -> float | None:
        return None if t is None else (now - t).total_seconds()
