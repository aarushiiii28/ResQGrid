"""M20 stable routes in the live app. Plan once and commit (a primary route plus up to two pre-checked backups), map-match every
GPS fix, and change the route only when the policy in routing_policy.py says it is clearly worth it. Same-destination
changes apply to the driver automatically, tell the dispatcher (reason and minutes saved) and are acknowledged with one tap.
A change of destination or vehicle is never made here: that still needs a human.

Every evaluation is written to the ledger with its reason codes, numbers and policy version."""

import math
import uuid
from datetime import datetime

from app.routing.base import DisruptionSet, build_steps, finish_estimate
from app.routing.geometry import M_PER_DEG, LatLon, point_at, polyline_length_m, project_along, tail_from
from app.routing.mock import SPEED_MPS
from app.schemas.nav import Backup, OffRoute, RouteLock, RouteNotice
from app.schemas.routing import RouteEstimate
from app.services import routing_policy as rp
from app.services.incident_service import RuleViolation

ACTIVE = ("accepted", "en_route", "transporting")


def _pl(route: RouteEstimate) -> list[LatLon]:
    return [(p[0], p[1]) for p in route.polyline]


class RouteStabilityService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.policy = rp.load_policy()

    @property
    def version(self) -> str:
        return self.policy["policy_version"]

    # ---- plan once, commit ----------------------------------------------------------------------------------------
    async def commit(self, vid: str, st: dict, here: LatLon, target: LatLon, disr: DisruptionSet, incident_id: str, phase: str) -> None:
        """Called when a route is first planned for a leg. Fetches up to two alternatives and pre-checks every one for closures."""
        ctx = self.ctx
        alts = await ctx.routing.alternatives(here, target, 2, disr)
        n = int(st.get("stab", {}).get("route_no", 0)) + 1
        backups = [Backup(id=f"BK-{vid}-{n}-{i + 1}", polyline=_pl(a), duration_s=a.duration_s, feasible=a.feasible and not disr.hit_closures(_pl(a)),
                          label=f"backup {i + 1}") for i, a in enumerate(alts)]
        st["stab"] = {"track": rp.TrackState(), "route_no": n, "route_id": f"RT-{vid}-{n}", "committed_at": ctx.now(), "backups": backups,
                      "commit_disr": DisruptionSet(list(disr.items)), "notice": None, "off": None, "last_eval_t": None, "prev_fix": None,
                      "accuracy": None, "phase": phase}
        route: RouteEstimate = st["route"]
        await ctx.audit.record(
            entity_type="nav", entity_id=vid, event_type="route.committed", actor="route-stability", correlation_id=incident_id,
            source="system", payload={"route_id": st["stab"]["route_id"], "version": 1, "policy_version": self.version, "phase": phase,
                                      "planned_eta_s": round(route.duration_s), "distance_m": round(route.distance_m),
                                      "backups": [{"id": b.id, "eta_s": round(b.duration_s), "feasible": b.feasible} for b in backups]},
            vis={"vehicle_id": vid})

    # ---- observing fixes -----------------------------------------------------------------------------------------------
    def _geom(self, st: dict, here: LatLon) -> tuple[list[LatLon], float, float, float]:
        pl = _pl(st["route"])
        along, cross = rp.route_progress(pl, here)
        return pl, along, cross, max(polyline_length_m(pl), 1.0)

    async def on_fix(self, vid: str, lat: float, lon: float, accuracy_m: float | None = None, heading: float | None = None) -> str | None:
        """Map-match one GPS fix, update the wrong-turn counters, and evaluate at once when a wrong turn is confirmed."""
        ctx = self.ctx
        st = ctx.nav.state.get(vid)
        if not st or "stab" not in st:
            return None
        stab = st["stab"]
        pl = _pl(st["route"])
        hints = [pl] + [list(b.polyline) for b in stab["backups"]]
        m = await ctx.routing.match([(lat, lon)], hints, self.policy["map_match"]["snap_radius_m"])
        mlat, mlon = m.points[0]
        # only snap onto the COMMITTED route: a fix near a backup is not "on route" (it may be a wrong turn onto the backup)
        cross_raw = rp.cross_track_m(pl, (lat, lon))
        cross_matched = rp.cross_track_m(pl, (mlat, mlon))
        cross = cross_matched if cross_raw <= self.policy["map_match"]["snap_radius_m"] else cross_raw
        hd = heading if heading is not None else rp.heading_from_fixes(stab["prev_fix"], (lat, lon))
        stab["prev_fix"], stab["accuracy"] = (lat, lon), accuracy_m
        before = stab["track"].off_count
        cls = rp.observe_fix(self.policy, stab["track"], cross_track=cross, accuracy_m=accuracy_m, heading_deg=hd)
        along = project_along(pl, (lat, lon))
        if cls == "uncertain":
            stab["off"] = OffRoute(state="uncertain", deviation_m=round(cross, 1),
                                   message="GPS accuracy is poor. The route will not be changed until the signal improves.")
        elif cls == "off_route":
            stab["off"] = OffRoute(state="off_route", deviation_m=round(cross, 1), message="Off route. Finding the quickest way back…")
        elif cls == "suspect":
            stab["off"] = OffRoute(state="suspect", deviation_m=round(cross, 1), message="Checking your position…")
        else:
            stab["off"] = None
        stab["last_along"], stab["last_cross"] = along, cross
        if cls == "off_route" and before < self.policy["off_route"]["consecutive_fixes"]:
            await self.evaluate(vid, force=True)
        return cls

    # ---- evaluation ------------------------------------------------------------------------------------------------------
    async def evaluate(self, vid: str, force: bool = False) -> rp.Decision | None:
        ctx = self.ctx
        if not self.policy.get("enabled", True):
            return None
        st = ctx.nav.state.get(vid)
        v = await ctx.vehicle_repo.get(vid)
        if not st or "stab" not in st or v is None or v.location is None or v.status not in ACTIVE:
            return None
        stab = st["stab"]
        track: rp.TrackState = stab["track"]
        now = ctx.now()
        t = now.timestamp()
        last = stab["last_eval_t"]
        if not force and last is not None and t - last < float(self.policy["evaluation"]["interval_s"]):
            return None
        stab["last_eval_t"] = t
        here = (v.location.lat, v.location.lon)
        route: RouteEstimate = st["route"]
        pl, along, cross, total = self._geom(st, here)
        remaining_m = max(0.0, total - along)
        tail = tail_from(pl, along)
        disr = await ctx.routing.active_disruptions()
        commit_disr: DisruptionSet = stab["commit_disr"]
        frac = remaining_m / total
        _, now_dur, _ = build_steps(tail, SPEED_MPS, disr)
        _, plan_dur, _ = build_steps(tail, SPEED_MPS, commit_disr)
        base = route.duration_s * frac
        scale = (base / plan_dur) if plan_dur > 0 else 1.0
        acc = stab.get("accuracy")
        off_conf = track.off_count >= self.policy["off_route"]["consecutive_fixes"] and not (acc is not None and acc > self.policy["off_route"]["max_accuracy_m"])
        obs = rp.Obs(t=t, remaining_m=remaining_m, cross_track_m=cross, accuracy_m=acc, remaining_eta_s=now_dur * scale,
                     planned_remaining_s=base, blocked=bool(disr.hit_closures(tail)), off_confirmed=off_conf)
        a = rp.assess(self.policy, track, obs)
        rejoin: list[rp.Option] | None = None
        replan: rp.Option | None = None
        alts: list[rp.Option] | None = None
        target = st["target"]
        if a.trigger in ("blocked", "off_route"):
            rejoin = await self._rejoin_options(here, pl, along, disr, route, scale)
            fresh = await ctx.routing.route(here, target, disr)
            replan = rp.Option(eta_s=fresh.duration_s, blocked=bool(disr.hit_closures(_pl(fresh))), label="full replan", payload=fresh)
        elif a.trigger == "congestion":
            fresh = await ctx.routing.route(here, target, disr)
            alts = [rp.Option(eta_s=fresh.duration_s, blocked=bool(disr.hit_closures(_pl(fresh))), label="fresh route", payload=fresh)]
            for alt in await ctx.routing.alternatives(here, target, 2, disr):
                alts.append(rp.Option(eta_s=alt.duration_s, blocked=bool(disr.hit_closures(_pl(alt))), label="alternative", payload=alt))
        d = rp.choose(self.policy, track, obs, a, rejoin=rejoin, replan=replan, alternatives=alts)
        d.numbers.update(route_id=stab["route_id"], route_version=track.version, reroutes=track.reroutes)
        inc_id = v.assigned_incident_id
        await ctx.audit.record(
            entity_type="nav", entity_id=vid, event_type="route.decision", actor="route-stability", correlation_id=inc_id, source="system",
            reason=", ".join(d.reasons), payload=d.as_log() | {"vehicle_id": vid}, vis={"vehicle_id": vid})
        if d.kind != "keep" and d.option is not None:
            await self._apply(vid, st, d, here, pl, obs, disr, inc_id)
        else:
            rp.apply_decision(track, d, t)
        return d

    async def _rejoin_options(self, here: LatLon, pl: list[LatLon], along: float, disr: DisruptionSet, route: RouteEstimate, scale: float) -> list[rp.Option]:
        return await rp.build_rejoin_options(self.policy, self.ctx.routing.route, here, pl, along, disr, scale, SPEED_MPS)

    # ---- applying a change --------------------------------------------------------------------------------------------------
    def _splice(self, conn: RouteEstimate, tail2: list[LatLon], tail_s: float, disr: DisruptionSet, now: datetime) -> RouteEstimate:
        new_pl = _pl(conn) + tail2[1:]
        tail_steps, _, _ = build_steps(tail2, SPEED_MPS, disr)
        steps = [s for s in conn.steps if s.maneuver != "arrive"] + tail_steps
        mid = conn.duration_s + tail_s
        return finish_estimate(pl=new_pl, steps=steps, mid_s=mid, source=conn.source, confidence=conn.confidence, low_f=0.9, high_f=1.25,
                               now=now, applied=list(conn.applied_disruptions), notes=["rejoined the original route"], degraded=conn.degraded)

    async def _apply(self, vid: str, st: dict, d: rp.Decision, here: LatLon, pl: list[LatLon], obs: rp.Obs, disr: DisruptionSet, inc_id: str | None) -> None:
        ctx = self.ctx
        stab, track = st["stab"], st["stab"]["track"]
        opt = d.option
        now = ctx.now()
        if d.kind == "rejoin":
            conn, ra, tail2, tail_s = opt.payload
            new_route = self._splice(conn, tail2, tail_s, disr, now)
            via = next((s.name for s in conn.steps if s.name), None)
            rejoin_in = round(polyline_length_m(_pl(conn)))
            first = "wrong_turn" if "OFF_ROUTE_CONFIRMED" in d.reasons else "blocked"
        else:
            new_route = opt.payload
            via, rejoin_in = None, None
            first = "blocked" if "ROAD_BLOCKED" in d.reasons else "wrong_turn" if "OFF_ROUTE_CONFIRMED" in d.reasons else "congestion"
        old_s = obs.remaining_eta_s
        saved = old_s - new_route.duration_s
        if first == "wrong_turn":
            msg = (f"Off route. Rejoin in {rejoin_in} m" + (f" via {via}" if via else "") + "." if d.kind == "rejoin" else "Off route. New route found.")
        elif first == "blocked":
            msg = "Road closed ahead. New route avoids it" + (f", {abs(saved) / 60:.1f} min {'faster' if saved > 0 else 'longer'}." if abs(saved) >= 6 else ".")
        else:
            msg = f"Heavy traffic ahead. New route is {saved / 60:.1f} min faster."
        n = await ctx.store.next_seq("reroute_seq")
        notice = RouteNotice(id=f"RN-{n:04d}", kind=d.kind, reason=first, message=msg, saved_s=round(saved, 1),  # type: ignore[arg-type]
                             old_remaining_s=round(old_s, 1), new_remaining_s=round(new_route.duration_s, 1), reason_codes=d.reasons, at=now)
        st["route"], st["sim_along"] = new_route, 0.0
        st["offset"] = None
        st["last_eta_push"] = None
        rp.apply_decision(track, d, obs.t)
        stab.update(notice=notice, off=None, committed_at=now, commit_disr=DisruptionSet(list(disr.items)))
        alts = await ctx.routing.alternatives(here, st["target"], 2, disr)
        stab["backups"] = [Backup(id=f"BK-{vid}-{stab['route_no']}-v{track.version}-{i + 1}", polyline=_pl(a), duration_s=a.duration_s,
                                  feasible=a.feasible and not disr.hit_closures(_pl(a)), label=f"backup {i + 1}") for i, a in enumerate(alts)]
        await ctx.audit.record(
            entity_type="nav", entity_id=vid, event_type="route.changed", actor="route-stability", correlation_id=inc_id, source="system",
            reason=msg, payload={"notice_id": notice.id, "kind": d.kind, "reason": first, "saved_s": notice.saved_s, "same_destination": True,
                                 "route_version": track.version, "reroutes": track.reroutes, "reason_codes": d.reasons,
                                 "policy_version": self.version, "note": "applied automatically; destination and vehicle unchanged"},
            vis={"vehicle_id": vid, "caller": (await ctx.incident_repo.get(inc_id)).created_by if inc_id else None})

    def note_external_change(self, vid: str, st: dict) -> None:
        """A dispatcher-approved reroute (M8) was accepted by the driver: the committed route changed, so the policy restarts from it."""
        stab = st.get("stab")
        if not stab:
            return
        tr: rp.TrackState = stab["track"]
        tr.version += 1
        tr.reroutes += 1
        tr.last_reroute_t = self.ctx.now().timestamp()
        tr.off_count = tr.congestion_count = 0
        stab["off"] = None

    async def acknowledge(self, vid: str, notice_id: str, actor: str) -> RouteNotice:
        ctx = self.ctx
        st = ctx.nav.state.get(vid)
        stab = st.get("stab") if st else None
        notice: RouteNotice | None = stab["notice"] if stab else None
        if notice is None or notice.id != notice_id:
            raise RuleViolation("no such route notice")
        if not notice.acknowledged:
            notice.acknowledged = True
            v = await ctx.vehicle_repo.get(vid)
            await ctx.audit.record(entity_type="nav", entity_id=vid, event_type="nav.route_notice_acknowledged", actor=actor,
                                   correlation_id=v.assigned_incident_id if v else None, source="human_confirmed",
                                   payload={"notice_id": notice.id, "kind": notice.kind, "note": "driver saw the change; this is an acknowledgment, not an approval"},
                                   vis={"vehicle_id": vid})
        return notice

    # ---- what the driver and dispatcher see ----------------------------------------------------------------------------------------
    def view(self, st: dict) -> dict:
        stab = st.get("stab")
        if not stab:
            return {}
        tr: rp.TrackState = stab["track"]
        relevant = bool(stab.get("off")) or tr.congestion_count > 0 or (stab["notice"] is not None and not stab["notice"].acknowledged)
        return {
            "route_lock": RouteLock(locked=True, version=tr.version, reroutes=tr.reroutes, policy_version=self.version,
                                    committed_at=stab["committed_at"]),
            "route_notice": stab["notice"],
            "off_route": stab["off"],
            "backups": stab["backups"] if relevant else [],
        }

    # ---- demo helper --------------------------------------------------------------------------------------------------------------------
    async def simulate_wrong_turn(self, vid: str, offset_m: float) -> dict:
        """Demo only: push the simulated GPS sideways so the vehicle is 'off route'. Labelled simulated."""
        ctx = self.ctx
        st = ctx.nav.state.get(vid)
        if not st or "stab" not in st:
            raise RuleViolation("no active route for this vehicle")
        pl = _pl(st["route"])
        along = st["sim_along"] if st.get("simulated") else 0.0
        a, b = point_at(pl, max(0.0, along - 10)), point_at(pl, along + 10)
        # direction of travel in local metres (east, north), then its left-hand perpendicular
        east = (b[1] - a[1]) * M_PER_DEG * math.cos(math.radians(a[0]))
        north = (b[0] - a[0]) * M_PER_DEG
        norm = math.hypot(east, north) or 1.0
        st["offset"] = (east / norm * offset_m, -north / norm * offset_m)  # (north_m, east_m)
        st["wrong_turn_at"] = ctx.now()
        await ctx.audit.record(entity_type="nav", entity_id=vid, event_type="nav.simulated_wrong_turn", actor="demo", source="simulated",
                               payload={"offset_m": offset_m, "note": "simulated wrong turn: GPS pushed off the route"}, vis={"vehicle_id": vid})
        return {"offset_m": offset_m, "simulated": True, "id": uuid.uuid4().hex[:8]}
