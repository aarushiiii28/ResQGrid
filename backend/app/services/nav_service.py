"""M17 driver navigation. Plans a leg to the scene (entrance node) or the hospital, follows it with
simulated GPS (labelled simulated) or real driver GPS, and proposes a reroute when a closure hits the
active route or the ETA drifts. A reroute is never applied silently: the driver accepts with one tap."""

import asyncio
import math
import uuid
from datetime import datetime

from app.routing.base import DisruptionSet, build_steps
from app.routing.geometry import (
    M_PER_DEG,
    LatLon,
    point_at,
    polyline_length_m,
    project_along,
    tail_from,
)
from app.routing.mock import SPEED_MPS
from app.schemas.common import GeoPoint
from app.schemas.nav import NavPlan, NavTarget, RerouteProposal, Signal
from app.schemas.routing import RouteEstimate
from app.schemas.vehicle import Vehicle, VehicleStateUpdate
from app.services.incident_service import RuleViolation

MIN_DRIFT_S = 45.0
MIN_BENEFIT = 0.85  # alternative must be at least 15% faster than the active route
ETA_PUSH_EVERY_S = 5.0


def slow_factor(tail: list[LatLon], disr: DisruptionSet) -> float:
    """How much slower the remaining path is under current disruptions than free-flowing."""
    if len(tail) < 2:
        return 1.0
    _, base, _ = build_steps(tail, SPEED_MPS, DisruptionSet())
    _, now, _ = build_steps(tail, SPEED_MPS, disr)
    return (now / base) if base > 0 else 1.0


class NavService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.state: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    # ---- targets --------------------------------------------------------------------
    async def _target(self, v: Vehicle, inc, disr: DisruptionSet):
        ctx = self.ctx
        if v.status in ("assigned", "accepted", "en_route"):
            pt, ent, _ = await ctx.dispatch.scene_point(inc, disr)
            name = "Incident location"
            if ent and ent.chosen:
                name = f"{ent.chosen.entrance.name}" + (f" ({ent.chosen.entrance.landmark})" if ent.chosen.entrance.landmark else "")
            return "scene", pt, name, ent
        h = await ctx.hospital_repo.get(inc.destination_id) if inc.destination_id else None
        if h is None:
            raise RuleViolation("no destination hospital set")
        return "hospital", (h.location.lat, h.location.lon), h.name, None

    # ---- plan -------------------------------------------------------------------------
    async def plan(self, vid: str) -> NavPlan | None:
        ctx = self.ctx
        v = await ctx.vehicle_repo.get(vid)
        if v is None or not v.assigned_incident_id or v.location is None:
            return None
        inc = await ctx.incident_repo.get(v.assigned_incident_id)
        if inc is None:
            return None
        disr = await ctx.routing.active_disruptions()
        phase, pt, name, ent = await self._target(v, inc, disr)
        here = (v.location.lat, v.location.lon)
        st = self.state.get(vid)
        if st is None or st["phase"] != phase or st["incident"] != inc.id:
            route = await ctx.routing.route(here, pt, disr)
            st = self.state[vid] = {"phase": phase, "incident": inc.id, "route": route, "proposal": None,
                                    "target": pt, "sim_along": 0.0, "last_eta_push": None, "declined": None,
                                    "simulated": False, "offset": None}
            await ctx.stability.commit(vid, st, here, pt, disr, inc.id, phase)  # M20: plan once, commit, keep backups
        route: RouteEstimate = st["route"]
        pl = [tuple(p) for p in route.polyline]
        total = max(polyline_length_m(pl), 1.0)
        along = st["sim_along"] if st["simulated"] else project_along(pl, here)
        remaining_m = max(0.0, total - along)
        tail = tail_from(pl, along)
        f = slow_factor(tail, disr)
        frac = remaining_m / total
        mid = route.duration_s * frac * f
        low = route.duration_low_s * frac * f
        high = route.duration_high_s * frac * f
        # steps: where is each maneuver along the polyline
        nxt, dist_next = None, None
        for s in route.steps:
            s_along = project_along(pl, (s.lat, s.lon)) if s.maneuver != "arrive" else total
            if s_along > along + 5:
                nxt, dist_next = s, s_along - along
                break
        sig = []
        d = 600.0
        while d < total - 150:
            la, lo = point_at(pl, d)
            sig.append(Signal(id=f"SIG-{len(sig) + 1}", lat=la, lon=lo, along_m=round(d, 1)))
            d += 600.0
        prop = st["proposal"] if st["proposal"] and st["proposal"].status == "pending" else None
        return NavPlan(
            vehicle_id=vid, incident_id=inc.id, phase=phase, vehicle_status=v.status,  # type: ignore[arg-type]
            target=NavTarget(kind=phase, name=name, lat=pt[0], lon=pt[1]),  # type: ignore[arg-type]
            route=route, along_m=round(along, 1), remaining_m=round(remaining_m, 1),
            remaining_low_s=round(low, 1), remaining_s=round(mid, 1), remaining_high_s=round(high, 1),
            next_step=nxt, distance_to_next_m=None if dist_next is None else round(dist_next, 1),
            last_100m=phase == "scene" and remaining_m <= 150, entrance=ent, reroute=prop,
            green_corridor={"simulation": True, "label": "Green corridor: simulation only. No real signal is controlled.",
                            "signals": [s.model_dump() for s in sig]},
            simulated_movement=st["simulated"],
            **ctx.stability.view(st),
        )

    # ---- movement (simulated GPS) -------------------------------------------------------------
    async def advance(self, vid: str, dt_s: float) -> dict:
        """Move the vehicle `dt_s` of real time along its active route (scaled by sim_speed_factor)."""
        ctx = self.ctx
        plan = await self.plan(vid)
        if plan is None:
            return {"done": True, "reason": "no plan"}
        st = self.state[vid]
        st["simulated"] = True
        pl = [tuple(p) for p in st["route"].polyline]
        total = max(polyline_length_m(pl), 1.0)
        along = min(total, st["sim_along"] + SPEED_MPS * ctx.settings.sim_speed_factor * dt_s)
        st["sim_along"] = along
        la, lo = point_at(pl, along)
        off = st.get("offset")
        if off:  # simulated wrong turn: the GPS is pushed sideways while the vehicle "drives on"
            la, lo = la + off[0] / M_PER_DEG, lo + off[1] / (M_PER_DEG * math.cos(math.radians(la)))
        now = ctx.now()
        await ctx.vehicles.apply_update(vid, VehicleStateUpdate(
            update_id=f"mv-{uuid.uuid4().hex[:12]}", occurred_at=now,
            location=GeoPoint(lat=la, lon=lo, accuracy_m=5), source="simulated"), "simulated-gps")
        await ctx.stability.on_fix(vid, la, lo, accuracy_m=5)  # map-match, wrong-turn counters
        await ctx.stability.evaluate(vid)  # throttled; logs every evaluation to the ledger
        await self._push_eta(vid, now)
        return {"done": along >= total - 0.5, "along_m": along, "total_m": total}

    async def _push_eta(self, vid: str, now: datetime, force: bool = False) -> None:
        st = self.state.get(vid)
        if not st:
            return
        last = st["last_eta_push"]
        if not force and last and (now - last).total_seconds() < ETA_PUSH_EVERY_S:
            return
        st["last_eta_push"] = now
        plan = await self.plan(vid)
        if not plan:
            return
        lo, hi = plan.remaining_low_s, plan.remaining_high_s
        if plan.phase == "scene":  # pre-alert ETA is arrival at the hospital: add the transport leg
            dec = None
            for d in await self.ctx.dispatch.repo.list():
                if d.incident_id == plan.incident_id and d.status in ("approved", "overridden"):
                    dec = d
            if dec and dec.recommendation:
                lo += dec.recommendation.eta_hospital_s * 0.85
                hi += dec.recommendation.eta_hospital_s * 1.25
        await self.ctx.cases.set_eta(plan.incident_id, lo, hi)

    async def drive(self, vid: str, tick_s: float | None = None) -> bool:
        """Run the active leg to its end. Returns False if the vehicle stopped being assigned."""
        ctx = self.ctx
        tick = tick_s or ctx.settings.sim_tick_s
        n = 0
        while True:
            r = await self.advance(vid, tick)
            n += 1
            if n % 6 == 0:
                await self.check_reroute(vid)
            if r["done"]:
                await self._push_eta(vid, ctx.now(), force=True)
                return r.get("reason") != "no plan"
            await asyncio.sleep(tick)

    def start_drive(self, vid: str) -> None:
        self.stop(vid)
        t = self.ctx.spawn(self.drive(vid))
        self.tasks[vid] = t

    def stop(self, vid: str) -> None:
        t = self.tasks.pop(vid, None)
        if t and not t.done():
            t.cancel()

    # ---- reroute ------------------------------------------------------------------------------
    async def check_reroute(self, vid: str) -> RerouteProposal | None:
        """M20: the stable-route policy decides (a closed road: reroute now, preferring to rejoin; congestion: only when it is
        worth it). A same-destination change is applied automatically with a driver notice and never becomes a pending
        proposal. A reroute that a dispatcher approved through the risk monitor (force_proposal) is still a proposal the
        driver taps."""
        st = self.state.get(vid)
        if not st:
            return None
        if st["proposal"] and st["proposal"].status == "pending":
            return st["proposal"]
        await self.ctx.stability.evaluate(vid, force=True)
        return None

    async def force_proposal(self, vid: str, reason: str, actor: str) -> RerouteProposal | None:
        """A dispatcher approved a reroute: put a fresh route in front of the driver. It is still
        applied only when the driver taps."""
        ctx = self.ctx
        await self.plan(vid)
        st = self.state.get(vid)
        v = await ctx.vehicle_repo.get(vid)
        if not st or v is None or v.location is None:
            return None
        if st["proposal"] and st["proposal"].status == "pending":
            return st["proposal"]
        disr = await ctx.routing.active_disruptions()
        route: RouteEstimate = st["route"]
        pl = [tuple(p) for p in route.polyline]
        total = max(polyline_length_m(pl), 1.0)
        here = (v.location.lat, v.location.lon)
        along = st["sim_along"] if st["simulated"] else project_along(pl, here)
        tail = tail_from(pl, along)
        old_now = route.duration_s * (max(0.0, total - along) / total) * slow_factor(tail, disr)
        fresh = await ctx.routing.route(here, st["target"], disr)
        n = await ctx.store.next_seq("reroute_seq")
        rsn = "closure" if disr.hit_closures(tail) else "eta_drift"
        prop = RerouteProposal(
            id=f"RR-{n:04d}", reason=rsn, message=f"Dispatcher approved a reroute: {reason}",  # type: ignore[arg-type]
            old_remaining_s=round(old_now, 1), new_remaining_s=round(fresh.duration_s, 1),
            delta_s=round(fresh.duration_s - old_now, 1), route=fresh)
        st["proposal"], st["proposal_sig"] = prop, (rsn, tuple(sorted(d.id for d in disr.items)))
        st["declined"] = None
        await ctx.audit.record(
            entity_type="nav", entity_id=vid, event_type="nav.reroute_proposed", actor=actor,
            correlation_id=v.assigned_incident_id, source="human_confirmed", reason=prop.message,
            payload={"proposal_id": prop.id, "reason": rsn, "delta_s": prop.delta_s, "approved_by_dispatcher": True},
            vis={"vehicle_id": vid})
        return prop

    async def check_all(self) -> list[RerouteProposal]:
        out = []
        for vid in list(self.state):
            p = await self.check_reroute(vid)
            if p and p.status == "pending":
                out.append(p)
        return out

    async def respond_reroute(self, vid: str, proposal_id: str, accept: bool, actor: str) -> RerouteProposal:
        ctx = self.ctx
        st = self.state.get(vid)
        prop = st["proposal"] if st else None
        if not prop or prop.id != proposal_id or prop.status != "pending":
            raise RuleViolation("no such pending reroute proposal")
        prop.status = "accepted" if accept else "declined"
        if accept:
            st["route"], st["sim_along"] = prop.route, 0.0
            st["last_eta_push"] = None
            st["offset"] = None
            ctx.stability.note_external_change(vid, st)
        else:
            st["declined"] = st.get("proposal_sig")
        v = await ctx.vehicle_repo.get(vid)
        await ctx.audit.record(
            entity_type="nav", entity_id=vid,
            event_type="nav.reroute_accepted" if accept else "nav.reroute_declined", actor=actor,
            correlation_id=v.assigned_incident_id if v else None, source="human_confirmed",
            payload={"proposal_id": prop.id, "reason": prop.reason}, vis={"vehicle_id": vid})
        st["proposal"] = prop
        return prop

    def reset(self, vid: str) -> None:
        self.stop(vid)
        self.state.pop(vid, None)

