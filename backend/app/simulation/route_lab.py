"""M20 in the Replay Lab: identical seeded trips driven under three route policies, using the real mock router and the same
routing_policy.py code the live app uses.

  A  reroute-on-every-change   replan whenever conditions change or the vehicle is off route (no hysteresis, no cooldown)
  B  never-reroute             keep the first route; a wrong turn is recovered by the driver alone; a closure costs a stop
  C  ResQGrid stable policy    plan once, rejoin first, reroute only when worth it (routing_policy.py + config/routing_policy.yaml)

A toy city, one vehicle, invented congestion that fluctuates, invented wrong turns and closures. All results are simulated and
describe this model, not real roads."""

import asyncio
import copy
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.routing.base import DisruptionSet, build_steps
from app.routing.geometry import M_PER_DEG, LatLon, offset_point, point_at, polyline_length_m, project_along, tail_from
from app.routing.mock import SPEED_MPS, MockRoutingProvider, _join
from app.schemas.routing import Disruption
from app.services import routing_policy as rp

LABEL = "SIMULATED: toy city, invented congestion, wrong turns and closures, one vehicle. Not real roads, not a prediction."
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CENTER: LatLon = (30.9010, 75.8573)
DT = 5.0              # seconds between GPS fixes
BUCKET = 30.0         # congestion changes every 30 s
MAX_T = 4 * 3600.0
POLICIES = ("A", "B", "C")
NAMES = {"A": "Reroute on every change", "B": "Never reroute", "C": "ResQGrid stable policy"}
TURNS = ("turn-left", "turn-right", "uturn", "roundabout")


@dataclass
class Zone:
    center: LatLon
    radius_m: float
    base: float
    amp: float
    period_s: float
    phase: float
    t0: float
    t1: float


@dataclass
class Scenario:
    seed: int
    start: LatLon
    dest: LatLon
    zones: list[Zone]
    closure: tuple[float, LatLon, float] | None          # (appears at t, centre, radius)
    wrong_turn: tuple[float, float] | None               # (at t, offset m)
    initial_closure: bool = False
    notes: list[str] = field(default_factory=list)


def make_scenario(seed: int) -> Scenario:
    r = random.Random(f"routes:{seed}")
    ang = r.uniform(0, 360)
    start = offset_point(CENTER, r.uniform(0, 360), r.uniform(0, 1500))
    dest = offset_point(start, ang, r.uniform(2500, 5500))
    # the geometry of the first route decides where congestion and closures go: use the mock router's own route
    pl = _join([start, dest], (0,))  # the mock router's undisturbed route: synchronous and deterministic
    total = polyline_length_m(pl)
    zones = []
    for _ in range(r.choice([2, 2, 3])):
        c = point_at(pl, total * r.uniform(0.2, 0.8))
        t0 = r.uniform(10, 200)
        # traffic that keeps changing: the multiplier swings by +-30..80 % around its mean with a period of 1 to 3 minutes
        zones.append(Zone(c, r.uniform(350, 800), r.uniform(1.8, 4.0), r.uniform(0.3, 0.8), r.uniform(60, 180), r.uniform(0, 6.28), t0, t0 + r.uniform(500, 1400)))
    closure = None
    if r.random() < 0.35:
        closure = (r.uniform(60, 260), point_at(pl, total * r.uniform(0.5, 0.9)), r.uniform(110, 240))
    wrong = (r.uniform(40, 260), r.uniform(150, 400)) if r.random() < 0.7 else None
    return Scenario(seed, start, dest, zones, closure, wrong)


def disruptions_at(sc: Scenario, t: float, ignore_closure: bool = False) -> DisruptionSet:
    items: list[Disruption] = []
    b = math.floor(t / BUCKET)
    for i, z in enumerate(sc.zones):
        if z.t0 <= t <= z.t1:
            m = z.base * (1 + z.amp * math.sin(2 * math.pi * (b * BUCKET) / z.period_s + z.phase))
            items.append(Disruption(id=f"Z{i}", kind="congestion", lat=z.center[0], lon=z.center[1], radius_m=z.radius_m,
                                    multiplier=max(1.0, min(10.0, m)), created_at=NOW))
    if sc.closure and t >= sc.closure[0] and not ignore_closure:
        items.append(Disruption(id="C0", kind="closure", lat=sc.closure[1][0], lon=sc.closure[1][1], radius_m=sc.closure[2], created_at=NOW))
    return DisruptionSet(items)


def _pl(route) -> list[LatLon]:
    return [(p[0], p[1]) for p in route.polyline]


def _turn_keys(steps, pl: list[LatLon], along_m: float) -> set:
    out = set()
    for s in steps:
        if s.maneuver in TURNS and project_along(pl, (s.lat, s.lon)) >= along_m - 5:
            out.add((round(s.lat, 4), round(s.lon, 4), s.maneuver))
    return out


class Trip:
    """One vehicle driving one scenario under one policy."""

    def __init__(self, policy: str, sc: Scenario, wrong_turn: bool, cfg: dict):
        self.policy, self.sc, self.cfg = policy, sc, cfg
        self.wrong_turn = wrong_turn and sc.wrong_turn is not None
        self.mock = MockRoutingProvider()
        self.track = rp.TrackState()
        self.reroutes = 0
        self.changed_turns = 0
        self.eta_series: list[float] = []
        self.off: tuple[float, float] | None = None       # (north_m, east_m) offset while off route
        self.off_since: float | None = None
        self.stall = 0.0
        self.closure_passed = False
        self.wrong_done = False

    async def route(self, a: LatLon, b: LatLon, disr: DisruptionSet):
        return await self.mock.route(a, b, disr, NOW)

    async def run(self) -> dict:
        sc = self.sc
        t = 0.0
        disr = disruptions_at(sc, 0.0)
        route = await self.route(sc.start, sc.dest, disr)
        pl, dur = _pl(route), route.duration_s
        commit_disr = disr
        along = 0.0
        pos = sc.start
        last_bucket = -1
        while t < MAX_T:
            disr = disruptions_at(sc, t, ignore_closure=(self.policy == "B"))
            total = max(polyline_length_m(pl), 1.0)
            if self.stall > 0:                                  # driver is lost or stopped: no progress
                self.stall -= DT
            else:
                mult, _ = disr.segment_multiplier(pos, pos)
                along = min(total, along + SPEED_MPS / max(mult, 1.0) * DT)
                if self.policy == "B" and sc.closure and not self.closure_passed and t >= sc.closure[0]:
                    if polyline_length_m([pos, sc.closure[1]]) < sc.closure[2]:
                        self.closure_passed, self.stall = True, 240.0  # blocked road with no re-routing: stop, ask around, find a way
            on_route_pos = point_at(pl, along)
            if self.wrong_turn and not self.wrong_done and self.off is None and t >= sc.wrong_turn[0] and 0.15 * total < along < 0.8 * total:
                a, b = point_at(pl, max(0.0, along - 10)), point_at(pl, along + 10)
                east = (b[1] - a[1]) * M_PER_DEG * math.cos(math.radians(a[0]))
                north = (b[0] - a[0]) * M_PER_DEG
                n = math.hypot(east, north) or 1.0
                self.off = (east / n * sc.wrong_turn[1], -north / n * sc.wrong_turn[1])
                self.off_since, self.wrong_done = t, True
                if self.policy == "B":                         # recovers alone: out and back, plus confusion
                    self.stall += 2 * sc.wrong_turn[1] / SPEED_MPS + 30.0
                    self.off = None
            pos = on_route_pos if self.off is None else (on_route_pos[0] + self.off[0] / M_PER_DEG, on_route_pos[1] + self.off[1] / (M_PER_DEG * math.cos(math.radians(on_route_pos[0]))))
            if along >= total - 1.0 and self.off is None:
                break
            along_p = project_along(pl, pos)
            tail = tail_from(pl, along_p)
            _, now_dur, _ = build_steps(tail, SPEED_MPS, disr)
            self.eta_series.append(t + now_dur)

            if self.policy == "A":
                bucket = math.floor(t / BUCKET)
                cross = rp.cross_track_m(pl, pos)
                off_route = cross > 45.0
                if off_route or bucket != last_bucket or disr.hit_closures(tail):
                    last_bucket = bucket
                    # same candidate routes as the stable policy sees, but no thresholds, hysteresis, cooldown or lock
                    cands = [await self.route(pos, sc.dest, disr), *await self.mock.alternatives(pos, sc.dest, disr, NOW, 2)]
                    clear = [x for x in cands if not disr.hit_closures(_pl(x))] or cands
                    best = min(clear, key=lambda x: x.duration_s)
                    if off_route or disr.hit_closures(tail) or best.duration_s < now_dur - 1.0:
                        self._count_change(route_steps_old=None, pl_old=pl, along_old=along_p, new_pl=_pl(best), disr=disr)
                        route, pl, dur, along, self.off = best, _pl(best), best.duration_s, 0.0, None
                        self.reroutes += 1
            elif self.policy == "C":
                changed = await self._stable_step(t, pos, pl, dur, commit_disr, disr)
                if changed:
                    pl, dur, along, self.off = changed
                    commit_disr = disr
            t += DT
        return {"total_time_s": round(t, 1), "reroutes": self.reroutes, "changed_turns": self.changed_turns,
                "eta_std_s": round(rp.mean_eta_error(self.eta_series), 1)}

    def _count_change(self, route_steps_old, pl_old, along_old, new_pl, disr) -> None:
        old_steps, _, _ = build_steps(tail_from(pl_old, along_old), SPEED_MPS, DisruptionSet())
        new_steps, _, _ = build_steps(new_pl, SPEED_MPS, DisruptionSet())
        old_keys = {(round(s.lat, 4), round(s.lon, 4), s.maneuver) for s in old_steps if s.maneuver in TURNS}
        new_keys = {(round(s.lat, 4), round(s.lon, 4), s.maneuver) for s in new_steps if s.maneuver in TURNS}
        self.changed_turns += len(new_keys - old_keys)

    async def _stable_step(self, t: float, pos: LatLon, pl: list[LatLon], dur: float, commit_disr, disr):
        P = self.cfg
        along, cross = rp.route_progress(pl, pos)
        rp.observe_fix(P, self.track, cross_track=cross, accuracy_m=6.0)
        total = max(polyline_length_m(pl), 1.0)
        remaining_m = max(0.0, total - along)
        tail = tail_from(pl, along)
        _, now_dur, _ = build_steps(tail, SPEED_MPS, disr)
        _, plan_dur, _ = build_steps(tail, SPEED_MPS, commit_disr)
        base = dur * remaining_m / total
        scale = base / plan_dur if plan_dur > 0 else 1.0
        off_conf = self.track.off_count >= P["off_route"]["consecutive_fixes"]
        obs = rp.Obs(t=t, remaining_m=remaining_m, cross_track_m=cross, accuracy_m=6.0, remaining_eta_s=now_dur * scale,
                     planned_remaining_s=base, blocked=bool(disr.hit_closures(tail)), off_confirmed=off_conf)
        a = rp.assess(P, self.track, obs)
        rejoin = replan = alts = None
        if a.trigger in ("blocked", "off_route"):
            rejoin = await rp.build_rejoin_options(P, self.route, pos, pl, along, disr, scale, SPEED_MPS)
            fresh = await self.route(pos, self.sc.dest, disr)
            replan = rp.Option(eta_s=fresh.duration_s, blocked=bool(disr.hit_closures(_pl(fresh))), label="replan", payload=fresh)
        elif a.trigger == "congestion":
            fresh = await self.route(pos, self.sc.dest, disr)
            alts = [rp.Option(eta_s=fresh.duration_s, blocked=bool(disr.hit_closures(_pl(fresh))), label="fresh", payload=fresh)]
            for alt in await self.mock.alternatives(pos, self.sc.dest, disr, NOW, 2):
                alts.append(rp.Option(eta_s=alt.duration_s, blocked=bool(disr.hit_closures(_pl(alt))), label="alt", payload=alt))
        d = rp.choose(P, self.track, obs, a, rejoin=rejoin, replan=replan, alternatives=alts)
        if d.kind == "keep" or d.option is None:
            rp.apply_decision(self.track, d, t)
            return None
        if d.kind == "rejoin":
            conn, _ra, tail2, tail_s = d.option.payload
            new_pl = _pl(conn) + tail2[1:]
            new_dur = conn.duration_s + tail_s
        else:
            new_pl, new_dur = _pl(d.option.payload), d.option.payload.duration_s
        self._count_change(None, pl, along, new_pl, disr)
        rp.apply_decision(self.track, d, t)
        self.reroutes += 1
        return new_pl, new_dur, 0.0, None


def _P(policy_cfg: dict | None) -> dict:
    return policy_cfg if policy_cfg is not None else rp.load_policy()


async def _trip(policy: str, sc: Scenario, wrong: bool, cfg: dict) -> dict:
    return await Trip(policy, sc, wrong, cfg).run()


async def _compare(seeds: list[int], cfg: dict) -> list[dict]:
    rows = []
    for seed in seeds:
        sc = make_scenario(seed)
        row = {"seed": seed, "has_wrong_turn": sc.wrong_turn is not None, "has_closure": sc.closure is not None,
               "congestion_zones": len(sc.zones), "policies": {}}
        for p in POLICIES:
            r = await _trip(p, sc, True, cfg)
            if sc.wrong_turn is not None:
                clean = await _trip(p, sc, False, cfg)
                r["wrong_turn_loss_s"] = round(max(0.0, r["total_time_s"] - clean["total_time_s"]), 1)
            else:
                r["wrong_turn_loss_s"] = None
            row["policies"][p] = r
        rows.append(row)
    return rows


METRICS = [("total_time_s", "Total travel time (s)"), ("reroutes", "Route changes"), ("eta_std_s", "ETA variability, std dev (s)"),
           ("changed_turns", "Turns changed"), ("wrong_turn_loss_s", "Time lost after a wrong turn (s)")]


def _summary(rows: list[dict]) -> dict:
    out: dict = {}
    for p in POLICIES:
        out[p] = {}
        for k, _ in METRICS:
            vals = [r["policies"][p][k] for r in rows if r["policies"][p][k] is not None]
            out[p][k] = {"mean": round(statistics.mean(vals), 1), "median": round(statistics.median(vals), 1), "n": len(vals)} if vals else {"mean": None, "median": None, "n": 0}
    return out


def _worse(rows: list[dict]) -> list[dict]:
    """Where the stable policy is worse than another policy, per seed, with the size of the difference."""
    out = []
    for other in ("A", "B"):
        for k, label in METRICS:
            diffs = [(r["policies"]["C"][k] - r["policies"][other][k]) for r in rows if r["policies"]["C"][k] is not None and r["policies"][other][k] is not None]
            worse = [d for d in diffs if d > 0.05]
            if worse:
                out.append({"vs": other, "vs_name": NAMES[other], "metric": k, "label": label, "seeds_worse": len(worse), "of": len(diffs),
                            "mean_delta": round(statistics.mean(worse), 1)})
    return out


def _sweep(seeds: list[int], base: dict) -> list[dict]:
    out = []
    grid = [("min_saving_s", v) for v in (0, 15, 30, 60, 90, 150)] + [("sustained_evaluations", v) for v in (1, 2, 3, 4)]
    for key, v in grid:
        cfg = copy.deepcopy(base)
        cfg["congestion"][key] = v
        if key == "min_saving_s":
            cfg["congestion"]["min_saving_pct"] = 0 if v == 0 else base["congestion"]["min_saving_pct"]
        rows = [asyncio.run(_one_c(s, cfg)) for s in seeds]
        out.append({"setting": key, "value": v, "current": v == base["congestion"][key],
                    "mean_reroutes": round(statistics.mean(r["reroutes"] for r in rows), 2),
                    "mean_total_time_s": round(statistics.mean(r["total_time_s"] for r in rows), 1),
                    "mean_eta_std_s": round(statistics.mean(r["eta_std_s"] for r in rows), 1)})
    return out


async def _one_c(seed: int, cfg: dict) -> dict:
    return await _trip("C", make_scenario(seed), True, cfg)


def run_compare(seed: int = 1, n: int = 8, policy_cfg: dict | None = None, sweep: bool = True) -> dict:
    cfg = _P(policy_cfg)
    seeds = list(range(seed, seed + n))
    rows = asyncio.run(_compare(seeds, cfg))
    return {
        "label": LABEL, "seeds": seeds, "policy_version": cfg["policy_version"], "names": NAMES,
        "metrics": [{"key": k, "label": label} for k, label in METRICS], "summary": _summary(rows), "per_seed": rows,
        "where_stable_is_worse": _worse(rows),
        "sweep": _sweep(seeds[: max(3, min(len(seeds), 6))], cfg) if sweep else [],
        "thresholds": {"congestion": cfg["congestion"], "off_route": cfg["off_route"], "rejoin": cfg["rejoin"]},
    }
