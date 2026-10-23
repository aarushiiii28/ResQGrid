"""Runs policies on identical worlds and aggregates across seeds."""

from dataclasses import asdict
from statistics import mean
from typing import Any

from app.simulation import metrics as mx
from app.simulation.engine import Sim
from app.simulation.model import ALL_FAULTS, SimConfig, build_world
from app.simulation.policies import POLICIES

LABEL = "SIMULATED: results describe a synthetic world, not real emergencies. No lives-saved claims."


def make_config(seed: int, faults: list[str] | tuple[str, ...] | None = None, **over: Any) -> SimConfig:
    fl = tuple(f for f in (ALL_FAULTS if faults is None else faults))
    bad = [f for f in fl if f not in ALL_FAULTS]
    if bad:
        raise ValueError(f"unknown fault(s): {', '.join(bad)}")
    return SimConfig(seed=seed, faults=fl, **over)


def world_summary(w) -> dict[str, Any]:
    return {
        "hospitals": [{"id": h.id, "lat": h.lat, "lon": h.lon, "caps": sorted(h.caps), "stops_reporting_at": h.stops_reporting_at,
                       "down": {c: [[round(a, 1), round(b, 1)] for a, b in ws] for c, ws in h.windows.items()}} for h in w.hospitals],
        "vehicles": [{"id": v.id, "lat": v.lat, "lon": v.lon, "tags": sorted(v.tags), "gps_gaps": [[round(a, 1), round(b, 1)] for a, b in v.gps_gaps],
                      "offline_at": v.offline_at} for v in w.vehicles],
        "incidents": [{"id": r.id, "t_call": r.t_call, "category": r.category, "priority": r.priority, "lat": r.lat, "lon": r.lon,
                       "duplicate_of": r.dup_of, "blocked_entrance": r.blocked_entrance, "no_ack_first_crew": r.noack_first,
                       "burst": r.burst} for r in w.reports],
        "closures": [{"id": c.id, "lat": c.lat, "lon": c.lon, "radius_m": round(c.radius), "t0": c.t0, "t1": c.t1} for c in w.closures],
    }


def run_one(cfg: SimConfig, policy: str, world=None, with_tracks: bool = True) -> dict[str, Any]:
    world = world or build_world(cfg)
    pol = POLICIES[policy]()
    sim = Sim(world, pol)
    sim.run()
    out = mx.compute(sim)
    res = {"policy": policy, "policy_version": pol.version, "description": pol.description, **out,
           "timeline": sim.timeline, "decisions": [list(d) for d in sim.decisions], "end_s": round(sim.max_t, 1)}
    if with_tracks:
        res["tracks"] = mx.tracks(sim)
    return res


def run_compare(seed: int, faults: list[str] | None = None, policies: list[str] | None = None, **over: Any) -> dict[str, Any]:
    cfg = make_config(seed, faults, **over)
    world = build_world(cfg)  # one world, replayed by every policy
    pols = policies or ["P0", "P1", "P2"]
    return {
        "label": LABEL, "seed": seed, "faults": list(cfg.faults), "config": {**asdict(cfg), "faults": list(cfg.faults)},
        "world": world_summary(world), "results": {p: run_one(cfg, p, world) for p in pols},
    }


def _dist(vals: list[float]) -> dict[str, Any]:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "mean": round(mean(v), 3), "median": round(mx.pct(v, 0.5), 3), "p10": round(mx.pct(v, 0.1), 3),
            "p90": round(mx.pct(v, 0.9), 3), "min": round(min(v), 3), "max": round(max(v), 3), "values": [round(x, 3) for x in v]}


def benchmark(seeds: list[int], faults: list[str] | None = None, policies: list[str] | None = None, **over: Any) -> dict[str, Any]:
    pols = policies or ["P0", "P1", "P2"]
    per: dict[str, dict[str, list[float | None]]] = {p: {k: [] for k in mx.KEY_METRICS} for p in pols}
    for s in seeds:
        cfg = make_config(s, faults, **over)
        world = build_world(cfg)
        for p in pols:
            m = run_one(cfg, p, world, with_tracks=False)["metrics"]
            for k in mx.KEY_METRICS:
                per[p][k].append(m.get(k))
    dist = {p: {k: _dist(per[p][k]) for k in mx.KEY_METRICS} for p in pols}
    paired: dict[str, Any] = {}
    for a, b in (("P1", "P0"), ("P2", "P0"), ("P2", "P1")):
        if a not in pols or b not in pols:
            continue
        cmp: dict[str, Any] = {}
        for k in mx.KEY_METRICS:
            diffs = [(x, y) for x, y in zip(per[a][k], per[b][k], strict=True) if x is not None and y is not None]
            if not diffs:
                continue
            lower = k in mx.LOWER_BETTER
            better = sum(1 for x, y in diffs if (x < y if lower else x > y))
            worse = sum(1 for x, y in diffs if (x > y if lower else x < y))
            deltas = [x - y for x, y in diffs]
            cmp[k] = {"better": better, "worse": worse, "tied": len(diffs) - better - worse, "mean_delta": round(mean(deltas), 3),
                      "median_delta": round(mx.pct(deltas, 0.5), 3), "lower_is_better": lower}
        paired[f"{a}_vs_{b}"] = cmp
    worse_for_p2 = []
    if "P2_vs_P0" in paired:
        for k, c in paired["P2_vs_P0"].items():
            if c["worse"] > 0:
                worse_for_p2.append({"metric": k, "label": mx.LABELS[k], "seeds_worse": c["worse"], "of": c["better"] + c["worse"] + c["tied"],
                                     "mean_delta": c["mean_delta"], "lower_is_better": c["lower_is_better"]})
    return {"label": LABEL, "seeds": seeds, "faults": list(faults if faults is not None else ALL_FAULTS), "policies": pols,
            "distributions": dist, "paired": paired, "where_p2_is_worse_than_p0": worse_for_p2, "labels": mx.LABELS}
