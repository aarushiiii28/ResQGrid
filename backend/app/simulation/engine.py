"""M11 deterministic event engine. A heap of timed events drives a truth model (vehicle motion,
acknowledgments, closures, hospital state, handover queues). A policy never touches that truth: it
gets a `PolicyContext` with (a) an Observation view that only returns what could have been reported
by time t, and (b) a small set of actions. That is how future events cannot leak to the policy."""

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

from app.core.geo import haversine_m
from app.optimization import demand as dm
from app.routing.geometry import path_hits_circle
from app.simulation.model import Closure, LatLon, SimIncident, World, manhattan_m

END_PAD_S = 6 * 3600.0
SIM_START_S = 8 * 3600.0  # the simulated day starts at 08:00


# ------------------------------------------------------------------------------------------------ truth
@dataclass
class VState:
    v: Any  # SimVehicle
    segs: list[tuple[float, float, LatLon, LatLon]] = field(default_factory=list)
    busy_with: "Assignment | None" = None

    def pos(self, t: float) -> LatLon:
        pos: LatLon = (self.v.lat, self.v.lon)
        for t0, t1, p0, p1 in self.segs:
            if t < t0:
                break
            if t >= t1 or t1 <= t0:
                pos = p1
            else:
                f = (t - t0) / (t1 - t0)
                pos = (p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f)
        return pos

    def offline(self, t: float) -> bool:
        return self.v.offline_at is not None and t >= self.v.offline_at


@dataclass
class Assignment:
    id: str
    report: SimIncident
    vid: str
    hid: str
    t_assign: float
    kind: str = "initial"  # initial | redispatch | preempt
    ver: int = 0
    acked_t: float | None = None
    noack: bool = False
    cancelled: bool = False
    cancel_reason: str = ""
    failed: bool = False
    stage: str = "pending_ack"  # pending_ack | to_scene | at_scene | to_hospital | done
    rerouted: bool = False
    entrance_known: bool = False
    pred: tuple[float, float] = (0.0, 0.0)  # predicted travel range to scene (s)
    t_depart: float | None = None
    base_travel: float = 0.0
    penalty: float = 0.0
    closure_hit: bool = False
    t_arrive_scene: float | None = None
    t_leave_scene: float | None = None
    t_arrive_hosp: float | None = None
    final_hid: str | None = None
    diverted: bool = False
    redirected: bool = False
    wasted: bool = False
    coverage: float | None = None
    prev_vid: str | None = None
    prev_healthy: bool = False
    entrance_requested: bool = False
    handover_s: float | None = None
    detections: dict[str, tuple[float, float]] = field(default_factory=dict)  # failure kind -> (detected_t, decided_t)


@dataclass(frozen=True)
class AssignmentRef:
    """Opaque handle a policy holds. It carries only what the dispatcher chose, never the hidden truth."""

    id: str
    report_id: str
    vid: str
    hid: str
    t_assign: float
    kind: str


@dataclass
class IncRec:
    first_call: float
    priority: int
    category: str
    t_scene: float | None = None
    t_final: float | None = None
    appropriate_first: bool | None = None
    vehicle_capable: bool | None = None
    vehicles: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ observation
@dataclass(frozen=True)
class ReportObs:
    id: str
    t_call: float
    category: str
    priority: int
    lat: float
    lon: float
    text_sig: str


@dataclass(frozen=True)
class VehicleObs:
    id: str
    tags: frozenset[str]
    lat: float
    lon: float
    age_s: float


@dataclass(frozen=True)
class HospitalObs:
    id: str
    lat: float
    lon: float
    caps: frozenset[str]
    down: frozenset[str]  # capabilities reported down at the last report
    age_s: float


def last_report_time(phase: float, period: float, t: float, gaps: list[tuple[float, float]], stop_at: float | None) -> float:
    """Latest report time <= t, skipping outages. Always <= t: observations cannot look ahead."""
    r = phase + math.floor((t - phase) / period) * period if t >= phase else phase - period
    if stop_at is not None:
        r = min(r, stop_at)
        r = phase + math.floor((r - phase) / period) * period if r >= phase else phase - period
    for _ in range(3):
        for a, b in gaps:
            if a <= r < b:
                r = phase + math.floor(((a - 1e-6) - phase) / period) * period if a > phase else phase - period
    return r


class View:
    """What a dispatcher could know at time `now`. No future, no hidden truth."""

    def __init__(self, sim: "Sim"):
        self._s = sim

    @property
    def now(self) -> float:
        return self._s.t

    def report(self, r: SimIncident) -> ReportObs:
        return ReportObs(r.id, r.t_call, r.category, r.priority, r.lat, r.lon, r.text_sig)

    def vehicles(self) -> list[VehicleObs]:
        s, t, cfg = self._s, self._s.t, self._s.cfg
        out = []
        for st in s.veh.values():
            v = st.v
            r = last_report_time(v.phase, cfg.gps_report_s, t, v.gps_gaps, v.offline_at)
            p = st.pos(max(r, 0.0)) if r >= 0 else (v.lat, v.lon)
            out.append(VehicleObs(v.id, v.tags, p[0], p[1], max(0.0, t - r)))
        return sorted(out, key=lambda x: x.id)

    def hospitals(self) -> list[HospitalObs]:
        s, t, cfg = self._s, self._s.t, self._s.cfg
        out = []
        for h in s.w.hospitals:
            r = last_report_time(h.phase, cfg.hospital_report_s, t, [], h.stops_reporting_at)
            down = frozenset(c for c in h.caps if h.down(c, max(r, 0.0)))
            out.append(HospitalObs(h.id, h.lat, h.lon, h.caps, down, max(0.0, t - r)))
        return out

    def closures(self) -> list[Closure]:
        t = self._s.t
        return [c for c in self._s.w.closures if t >= c.t0 + self._s.cfg.closure_known_after_s and t < c.t1]

    def crew_status(self, ref: AssignmentRef) -> dict[str, Any]:
        """Crew status messages the CAD would show. Acks and arrivals are visible once they happen."""
        a = self._s.by_id[ref.id]
        return {"acked": a.acked_t is not None and a.acked_t <= self._s.t, "arrived": a.stage in ("at_scene", "to_hospital", "done"), "done": a.stage == "done",
                "cancelled": a.cancelled}

    def unit_free_at(self, vid: str) -> bool:
        return self._s.veh[vid].busy_with is None


class PolicyContext:
    def __init__(self, sim: "Sim"):
        self._s = sim
        self.view = View(sim)
        self.cfg = sim.cfg

    @property
    def t(self) -> float:
        return self._s.t

    def coverage(self, points: list[LatLon]) -> float:
        """Demand-weighted coverage of the public demand forecast by units at these positions."""
        b = dm.bucket_index(int((self._s.t + SIM_START_S) // 3600) % 24)
        return dm.coverage(points, self._s.w.demand_cells, self._s.w.demand_weights[b], self._s.cfg.coverage_radius_m)

    # ---- actions ----------------------------------------------------------------------------
    def dispatch(self, report_id: str, vid: str, hid: str, pred: tuple[float, float], kind: str = "initial",
                 prev: AssignmentRef | None = None, entrance_confirm: bool = False) -> AssignmentRef:
        a = self._s.do_dispatch(self._s.reports[report_id], vid, hid, pred, kind, self._s.by_id[prev.id] if prev else None, entrance_confirm)
        return self._s.ref(a)

    def release(self, ref: AssignmentRef, reason: str) -> None:
        self._s.do_release(self._s.by_id[ref.id], reason)

    def reroute(self, ref: AssignmentRef) -> bool:
        return self._s.do_reroute(self._s.by_id[ref.id])

    def redirect(self, ref: AssignmentRef, hid: str) -> None:
        self._s.do_redirect(self._s.by_id[ref.id], hid)

    def timer(self, delay: float, name: str, *args: Any) -> None:
        self._s.push(self._s.t + delay, "timer", (name, args))

    def radio_check(self, ref: AssignmentRef) -> tuple[bool, bool]:
        """Phone or radio the crew: (has acknowledged, is responsive). A human action with a cost in policy time."""
        a = self._s.by_id[ref.id]
        return a.acked_t is not None, not self._s.veh[a.vid].offline(self._s.t)

    def confirm_hospital(self, hid: str, caps: set[str]) -> bool:
        """Phone the hospital: is it truly able to receive right now?"""
        h = next(h for h in self._s.w.hospitals if h.id == hid)
        return not any(h.down(c, self._s.t) for c in caps)

    def note_detection(self, ref: AssignmentRef, kind: str, detected_t: float, decided_t: float) -> None:
        self._s.by_id[ref.id].detections.setdefault(kind, (detected_t, decided_t))

    def log(self, kind: str, report_id: str | None, detail: str, **extra: Any) -> None:
        self._s.log(kind, report_id, detail, **extra)


# ------------------------------------------------------------------------------------------------- sim
class Sim:
    def __init__(self, world: World, policy: Any):
        self.w, self.cfg, self.policy = world, world.cfg, policy
        self.t = 0.0
        self.heap: list[tuple[float, int, str, Any]] = []
        self.n = 0
        self.veh = {v.id: VState(v) for v in world.vehicles}
        self.assignments: list[Assignment] = []
        self.by_id: dict[str, Assignment] = {}
        self.reports = {r.id: r for r in world.reports}
        self.timeline: list[dict[str, Any]] = []
        self.decisions: list[tuple[float, str, str, str]] = []
        self.inc: dict[str, IncRec] = {}
        self.hosp_end: dict[str, list[float]] = {h.id: [] for h in world.hospitals}
        self.hosp = {h.id: h for h in world.hospitals}
        self.ctx = PolicyContext(self)
        self.policy.bind(self.ctx)
        self.max_t = 0.0

    # ---- plumbing ------------------------------------------------------------------------------
    def ref(self, a: Assignment) -> AssignmentRef:
        return AssignmentRef(a.id, a.report.id, a.vid, a.hid, a.t_assign, a.kind)

    def push(self, t: float, kind: str, data: Any) -> None:
        self.n += 1
        heapq.heappush(self.heap, (t, self.n, kind, data))

    def log(self, kind: str, report_id: str | None, detail: str, **extra: Any) -> None:
        self.timeline.append({"t": round(self.t, 1), "kind": kind, "incident": report_id, "detail": detail, **extra})

    def travel_s(self, a: LatLon, b: LatLon, noise: float = 1.0) -> float:
        return manhattan_m(a, b) / self.cfg.speed_mps * noise

    def active_closure_on(self, a: LatLon, b: LatLon, t_mid: float) -> Closure | None:
        path = [a, (a[0], b[1]), b]
        for c in self.w.closures:
            if c.t0 <= t_mid < c.t1 and path_hits_circle(path, (c.lat, c.lon), c.radius):
                return c
        return None

    # ---- actions executed for the policy ----------------------------------------------------------------
    def do_dispatch(self, rep: SimIncident, vid: str, hid: str, pred: tuple[float, float], kind: str,
                    prev: Assignment | None, entrance_confirm: bool) -> Assignment:
        st = self.veh[vid]
        if st.busy_with is not None:
            raise RuntimeError(f"{vid} is busy in truth: a policy dispatched a unit it should not have believed free")
        a = Assignment(f"A{len(self.assignments) + 1:03d}", rep, vid, hid, self.t, kind=kind, pred=pred)
        if prev is not None:
            a.prev_vid, a.prev_healthy = prev.vid, prev.prev_healthy
        # truth: does this crew ever answer?
        first_for_incident = not any(x.report.real_id == rep.real_id for x in self.assignments)
        a.noack = (rep.noack_first and first_for_incident and rep.dup_of is None) or st.offline(self.t + self.cfg.ack_delay_range[1])
        a.entrance_requested = entrance_confirm
        # coverage left behind by this dispatch (truth positions of idle, online units)
        idle = [s.pos(self.t) for s in self.veh.values() if s.busy_with is None and not s.offline(self.t) and s.v.id != vid]
        b = dm.bucket_index(int((self.t + SIM_START_S) // 3600) % 24)
        a.coverage = dm.coverage(idle, self.w.demand_cells, self.w.demand_weights[b], self.cfg.coverage_radius_m)
        st.busy_with = a
        self.assignments.append(a)
        self.by_id[a.id] = a
        rec = self.inc.setdefault(rep.real_id, IncRec(rep.t_call, rep.priority, rep.category))
        rec.vehicles.append(vid)
        self.decisions.append((round(self.t, 1), rep.id, vid, hid))
        self.log("dispatch", rep.id, f"{vid} -> {hid}", vehicle=vid, hospital=hid, assignment=a.id, how=kind)
        if not a.noack:
            u = (self.cfg.ack_delay_range[0] + (hash_unit(self.cfg.seed, a.id)) * (self.cfg.ack_delay_range[1] - self.cfg.ack_delay_range[0]))
            self.push(self.t + u, "ack", (a.id, a.ver))
        self.push(self.t + self.cfg.manual_recheck_s, "recheck", a.id)
        self.policy.on_dispatch(self.ref(a))
        return a

    def _free(self, a: Assignment) -> None:
        st = self.veh[a.vid]
        if st.busy_with is a:
            st.busy_with = None

    def do_release(self, a: Assignment, reason: str) -> None:
        if a.cancelled or a.stage in ("at_scene", "to_hospital", "done"):
            return
        a.cancelled, a.cancel_reason = True, reason
        a.ver += 1
        st = self.veh[a.vid]
        here = st.pos(self.t)
        st.segs = [s for s in st.segs if s[0] < self.t]
        if st.segs:
            t0, t1, p0, p1 = st.segs[-1]
            if t1 > self.t:
                st.segs[-1] = (t0, self.t, p0, here)
        st.segs.append((self.t, self.t, here, here))
        a.prev_healthy = a.acked_t is not None and not a.failed and not st.offline(self.t) and reason != "preempt"
        self._free(a)
        self.log("release", a.report.id, f"{a.vid} released ({reason})", vehicle=a.vid, assignment=a.id)
        self.push(self.t, "unit_free", a.vid)

    def do_reroute(self, a: Assignment) -> bool:
        """Applies only if the unit has not yet reached the closure it would cross."""
        if a.cancelled or a.stage != "to_scene" or not a.closure_hit or a.rerouted:
            return False
        plain = a.base_travel
        if self.t >= a.t_depart + 0.5 * plain:
            self.log("reroute_too_late", a.report.id, f"{a.vid} already past the closure", vehicle=a.vid)
            return False
        a.rerouted, a.penalty = True, 0.10 * plain
        a.ver += 1
        arr = a.t_depart + plain + a.penalty
        a.t_arrive_scene = arr
        st = self.veh[a.vid]
        st.segs = [s for s in st.segs if s[0] < a.t_depart] + [(a.t_depart, arr, st.pos(a.t_depart), (a.report.lat, a.report.lon))]
        self.push(arr, "arrive_scene", (a.id, a.ver))
        self.log("reroute", a.report.id, f"{a.vid} rerouted around the closure (+10% travel)", vehicle=a.vid, assignment=a.id)
        return True

    def do_redirect(self, a: Assignment, hid: str) -> None:
        if a.cancelled or a.stage == "done" or hid == a.hid:
            return
        old = a.hid
        a.hid, a.redirected = hid, True
        if a.stage == "to_hospital":
            st = self.veh[a.vid]
            here = st.pos(self.t)
            h = self.hosp[hid]
            arr = self.t + self.travel_s(here, (h.lat, h.lon), a.report.noise_hosp)
            a.ver += 1
            st.segs = [s for s in st.segs if s[0] < self.t] + [(self.t, arr, here, (h.lat, h.lon))]
            self.push(arr, "arrive_hosp", (a.id, a.ver))
        self.log("redirect", a.report.id, f"destination {old} -> {hid}", vehicle=a.vid, assignment=a.id)

    # ---- truth transitions ------------------------------------------------------------------------------
    def on_ack(self, a: Assignment) -> None:
        st = self.veh[a.vid]
        if a.cancelled or st.offline(self.t):
            return
        a.acked_t = self.t
        a.stage = "to_scene"
        a.t_depart = self.t
        scene = (a.report.lat, a.report.lon)
        p0 = st.pos(self.t)
        a.base_travel = self.travel_s(p0, scene, a.report.noise_scene)
        c = self.active_closure_on(p0, scene, self.t + 0.5 * a.base_travel)
        a.closure_hit = c is not None
        a.penalty = max(240.0, 0.6 * a.base_travel) if c else 0.0
        arr = self.t + a.base_travel + a.penalty
        off = st.v.offline_at
        if off is not None and off < arr:  # the crew drops off the network on the way and stops
            a.failed = True
            f = max(0.0, (off - self.t) / max(arr - self.t, 1e-9))
            here = (p0[0] + (scene[0] - p0[0]) * f, p0[1] + (scene[1] - p0[1]) * f)
            st.segs.append((self.t, off, p0, here))
            self.log("unit_lost", a.report.id, f"{a.vid} went offline en route", vehicle=a.vid, assignment=a.id)
            return
        a.t_arrive_scene = arr
        st.segs.append((self.t, arr, p0, scene))
        self.push(arr, "arrive_scene", (a.id, a.ver))
        self.log("ack", a.report.id, f"{a.vid} acknowledged and left", vehicle=a.vid, assignment=a.id)
        self.policy.on_ack(self.ref(a))

    def on_arrive_scene(self, a: Assignment) -> None:
        st = self.veh[a.vid]
        rec = self.inc[a.report.real_id]
        scene = (a.report.lat, a.report.lon)
        if rec.t_scene is not None:  # someone else got there first: this dispatch was not needed
            a.wasted, a.stage = True, "done"
            self._free(a)
            self.log("wasted", a.report.id, f"{a.vid} arrived at an incident already attended", vehicle=a.vid)
            self.policy.on_arrive(self.ref(a))
            self.push(self.t, "unit_free", a.vid)
            return
        rec.t_scene = self.t
        rec.vehicle_capable = self._capable(st.v.tags, a.report.category)
        a.stage = "at_scene"
        a.t_arrive_scene = self.t
        a.entrance_known = a.entrance_requested and (self.t - a.t_assign) > self.cfg.entrance_confirm_s  # caller confirmed in time?
        extra = self.cfg.blocked_entrance_delay_s if (a.report.blocked_entrance and not a.entrance_known) else 0.0
        if extra:
            self.log("entrance_blocked", a.report.id, f"entrance blocked: +{int(extra)}s", vehicle=a.vid)
        leave = self.t + extra + self.cfg.scene_time_s
        a.t_leave_scene = leave
        st.segs.append((self.t, leave, scene, scene))
        self.log("arrive_scene", a.report.id, f"{a.vid} on scene", vehicle=a.vid, response_s=round(self.t - rec.first_call, 1))
        self.policy.on_arrive(self.ref(a))
        self.push(leave, "depart_hosp", (a.id, a.ver))

    def _capable(self, tags: frozenset[str], cat: str) -> bool:
        from app.core.capabilities import vehicle_satisfies

        return vehicle_satisfies(list(tags), self.w.req_tags(cat))

    def on_depart_hosp(self, a: Assignment) -> None:
        st = self.veh[a.vid]
        h = self.hosp[a.hid]
        scene = (a.report.lat, a.report.lon)
        a.stage = "to_hospital"
        arr = self.t + self.travel_s(scene, (h.lat, h.lon), a.report.noise_hosp)
        st.segs.append((self.t, arr, scene, (h.lat, h.lon)))
        self.push(arr, "arrive_hosp", (a.id, a.ver))

    def on_arrive_hosp(self, a: Assignment) -> None:
        st = self.veh[a.vid]
        rec = self.inc[a.report.real_id]
        caps = self.w.req_caps(a.report.category)
        h = self.hosp[a.hid]
        a.t_arrive_hosp = self.t
        fin, tfin = h, self.t
        if any(h.down(c, self.t) for c in caps):  # diverted: the hospital cannot take the patient
            a.diverted = True
            alts = [x for x in self.w.hospitals if x.id != h.id and x.caps >= caps and not any(x.down(c, self.t) for c in caps)]
            alts.sort(key=lambda x: (manhattan_m((h.lat, h.lon), (x.lat, x.lon)), x.id))
            if alts:
                fin = alts[0]
                tfin = self.t + self.travel_s((h.lat, h.lon), (fin.lat, fin.lon), a.report.noise_hosp) + self.cfg.diversion_penalty_s
                st.segs.append((self.t, tfin, (h.lat, h.lon), (fin.lat, fin.lon)))
            self.log("diverted", a.report.id, f"{h.id} unavailable on arrival; diverted to {fin.id}", vehicle=a.vid, hospital=fin.id)
        a.final_hid = fin.id
        rec.appropriate_first = rec.appropriate_first if rec.appropriate_first is not None else (not a.diverted)
        rec.t_final = tfin
        queue = sum(1 for e in self.hosp_end[fin.id] if e > tfin)
        end = tfin + a.report.handover_base + 120.0 * queue
        self.hosp_end[fin.id].append(end)
        a.stage = "done"
        self.log("arrive_hospital", a.report.id, f"{a.vid} at {fin.id}; handover {int(end - tfin)}s", vehicle=a.vid, hospital=fin.id,
                 handover_s=round(end - tfin, 1))
        a.handover_s = round(end - tfin, 1)
        st.segs.append((tfin, end, (fin.lat, fin.lon), (fin.lat, fin.lon)))
        self.push(end, "free_after_handover", a.id)

    # ---- main loop ------------------------------------------------------------------------------------------
    def run(self) -> None:
        for r in self.w.reports:
            self.push(r.t_call, "call", r)
            self.push(r.t_call + self.cfg.callback_s, "callback", r)
        if self.policy.monitor:
            t = self.cfg.monitor_tick_s
            while t <= self.cfg.horizon_s + 3600:
                self.push(t, "tick", None)
                t += self.cfg.monitor_tick_s
        while self.heap:
            t, _, kind, data = heapq.heappop(self.heap)
            self.t = max(self.t, t)
            self.max_t = max(self.max_t, self.t)
            if self.t > self.cfg.horizon_s + END_PAD_S:
                break
            if kind == "call":
                self.inc.setdefault(data.real_id, IncRec(data.t_call, data.priority, data.category))
                self.log("call", data.id, f"{data.category} P{data.priority}" + (f" (another call about {data.dup_of})" if data.dup_of else ""))
                self.policy.on_call(self.ctx.view.report(data))
            elif kind in ("ack", "arrive_scene", "depart_hosp", "arrive_hosp"):
                aid, ver = data
                a = self.by_id[aid]
                if a.cancelled or a.ver != ver:
                    continue
                {"ack": self.on_ack, "arrive_scene": self.on_arrive_scene, "depart_hosp": self.on_depart_hosp,
                 "arrive_hosp": self.on_arrive_hosp}[kind](a)
            elif kind == "free_after_handover":
                a = self.by_id[data]
                self._free(a)
                self.push(self.t, "unit_free", a.vid)
            elif kind == "unit_free":
                if self.veh[data].busy_with is None:  # stale notice: the unit was re-tasked since
                    self.policy.on_unit_free(data)
            elif kind == "recheck":
                a = self.by_id[data]
                if not a.cancelled and a.stage in ("pending_ack", "to_scene") and self.inc[a.report.real_id].t_scene is None:
                    self.policy.on_recheck(self.ref(a))
            elif kind == "callback":
                rec = self.inc.get(data.real_id)
                if rec is not None and rec.t_scene is None and data.dup_of is None:
                    self.policy.on_callback(self.ctx.view.report(data))
            elif kind == "timer":
                name, args = data
                self.policy.on_timer(name, *args)
            elif kind == "tick":
                self.policy.on_tick()


def hash_unit(seed: int, key: str) -> float:
    """Deterministic value in [0,1) from (seed, key): the same assignment id always gets the same crew delay."""
    import hashlib

    h = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(h[:6], "big") / 2**48


def eta_straight(a: LatLon, b: LatLon, speed: float) -> float:
    return haversine_m(a[0], a[1], b[0], b[1]) / speed
