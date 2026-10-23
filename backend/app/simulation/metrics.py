"""Metrics computed from the simulator's TRUTH after a run. SIMULATED results only: they describe the
toy world and policies in this file, not real emergency outcomes. No lives-saved claims."""

import math
from statistics import mean, median
from typing import Any

from app.simulation.engine import Assignment, Sim

# metrics where a LOWER value is better (used for win/loss comparisons)
LOWER_BETTER = {
    "response_median_s", "response_p90_s", "p1_response_median_s", "dest_median_s", "dest_p90_s", "unserved", "wasted_dispatches",
    "unnecessary_reassignments", "oscillations", "late_acks", "missed_acks", "dispatch_to_accept_median_s", "arrival_to_handover_median_s",
    "uncovered_demand", "detect_median_s", "decide_median_s", "diversions", "preemptions",
}
HIGHER_BETTER = {"pct_meeting_requirements", "coverage_mean", "eta_calibration", "detection_rate"}
KEY_METRICS = [
    "response_median_s", "response_p90_s", "p1_response_median_s", "dest_median_s", "dest_p90_s", "pct_meeting_requirements",
    "unserved", "wasted_dispatches", "unnecessary_reassignments", "oscillations", "preemptions", "late_acks", "missed_acks",
    "dispatch_to_accept_median_s", "arrival_to_handover_median_s", "coverage_mean", "eta_calibration", "diversions",
    "detection_rate", "detect_median_s", "decide_median_s",
]
LABELS = {
    "response_median_s": "Response time, median (s)", "response_p90_s": "Response time, p90 (s)",
    "p1_response_median_s": "Response time, P1 calls, median (s)", "dest_median_s": "Time to appropriate destination, median (s)",
    "dest_p90_s": "Time to appropriate destination, p90 (s)", "pct_meeting_requirements": "Meeting requirements (%)",
    "unserved": "Calls never attended", "wasted_dispatches": "Wasted dispatches", "unnecessary_reassignments": "Unnecessary reassignments",
    "oscillations": "Plan oscillations", "preemptions": "Deliberate hand-overs", "late_acks": "Late acknowledgments (>60 s)",
    "missed_acks": "Missed acknowledgments", "dispatch_to_accept_median_s": "Dispatch to accept, median (s)",
    "arrival_to_handover_median_s": "Arrival to handover, median (s)", "coverage_mean": "Coverage after dispatch (share of demand)",
    "eta_calibration": "ETA range calibration (share inside)", "diversions": "Hospital diversions",
    "detection_rate": "Failures detected (share)", "detect_median_s": "Time to detect failure, median (s)",
    "decide_median_s": "Time to recovery decision, median (s)",
}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _round(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


def failures_of(sim: Sim) -> list[dict[str, Any]]:
    out = []
    for a in sim.assignments:
        if a.cancelled and a.cancel_reason == "preempt":
            continue
        truth: list[tuple[str, float, set[str]]] = []
        if a.noack:
            truth.append(("no_ack", a.t_assign, {"no_ack", "unresponsive"}))
        if a.failed:
            truth.append(("unresponsive", a.t_depart or a.t_assign, {"unresponsive", "no_ack"}))
        if a.closure_hit:
            truth.append(("closure", a.t_depart or a.t_assign, {"closure"}))
        if a.diverted or a.redirected:
            truth.append(("hospital", a.t_assign, {"hospital"}))
        for kind, onset, accept in truth:
            hit = [v for k, v in a.detections.items() if k in accept]
            det = min(hit, key=lambda x: x[0]) if hit else None
            out.append({"kind": kind, "assignment": a.id, "incident": a.report.id, "onset_s": round(onset, 1),
                        "detected_s": None if det is None else round(det[0] - onset, 1),
                        "decided_s": None if det is None else round(det[1] - onset, 1)})
    return out


def compute(sim: Sim) -> dict[str, Any]:
    cfg, end = sim.cfg, sim.max_t
    rows: list[dict[str, Any]] = []
    for rid in sorted(sim.inc):
        rec = sim.inc[rid]
        served = rec.t_scene is not None
        resp = (rec.t_scene - rec.first_call) if served else (end - rec.first_call)
        dest = (rec.t_final - rec.first_call) if rec.t_final is not None else (end - rec.first_call)
        meets = bool(served and rec.vehicle_capable and rec.appropriate_first)
        rows.append({"incident": rid, "category": rec.category, "priority": rec.priority, "t_call": round(rec.first_call, 1),
                     "served": served, "response_s": round(resp, 1), "dest_s": round(dest, 1), "meets_requirements": meets,
                     "vehicle_capable": rec.vehicle_capable, "appropriate_first": rec.appropriate_first, "vehicles": rec.vehicles})
    resp = [r["response_s"] for r in rows]
    p1 = [r["response_s"] for r in rows if r["priority"] == 1]
    dest = [r["dest_s"] for r in rows]
    A: list[Assignment] = sim.assignments
    init = [a for a in A if a.kind == "initial"]
    acked_delay = [a.acked_t - a.t_assign for a in A if a.acked_t is not None and not a.cancelled]
    late = sum(1 for a in A if (a.acked_t is None and not (a.cancelled and a.cancel_reason == "preempt")) or (a.acked_t is not None and a.acked_t - a.t_assign > cfg.ack_timeout_s))
    missed = sum(1 for a in A if a.acked_t is None and not (a.cancelled and a.cancel_reason == "preempt"))
    hand = [a.handover_s for a in A if a.handover_s is not None and not a.wasted]
    cov = [a.coverage for a in A if a.coverage is not None and a.kind == "initial"]
    inside = []
    for a in A:
        if a.t_arrive_scene is not None and a.t_depart is not None and not a.cancelled:
            actual = a.t_arrive_scene - a.t_depart
            inside.append(1.0 if a.pred[0] <= actual <= a.pred[1] else 0.0)
    redis = [a for a in A if a.kind == "redispatch"]
    unnecessary = sum(1 for a in redis if a.prev_healthy)
    pre = sum(1 for a in A if a.kind == "preempt")
    osc = 0
    for rec in sim.inc.values():
        v = rec.vehicles
        osc += sum(1 for i in range(2, len(v)) if v[i] == v[i - 2] and v[i] != v[i - 1])
    fails = failures_of(sim)
    det = [f for f in fails if f["detected_s"] is not None]
    m: dict[str, Any] = {
        "incidents": len(rows),
        "response_median_s": _round(median(resp) if resp else None, 1), "response_p90_s": _round(pct(resp, 0.9), 1),
        "p1_response_median_s": _round(median(p1) if p1 else None, 1),
        "dest_median_s": _round(median(dest) if dest else None, 1), "dest_p90_s": _round(pct(dest, 0.9), 1),
        "pct_meeting_requirements": _round(100 * sum(r["meets_requirements"] for r in rows) / len(rows) if rows else None, 1),
        "unserved": sum(1 for r in rows if not r["served"]),
        "wasted_dispatches": sum(1 for a in A if a.wasted),
        "unnecessary_reassignments": unnecessary, "oscillations": osc, "preemptions": pre,
        "late_acks": late, "missed_acks": missed,
        "dispatch_to_accept_median_s": _round(median(acked_delay) if acked_delay else None, 1),
        "arrival_to_handover_median_s": _round(median(hand) if hand else None, 1),
        "coverage_mean": _round(mean(cov) if cov else None, 4), "uncovered_demand": _round(1 - mean(cov) if cov else None, 4),
        "eta_calibration": _round(mean(inside) if inside else None, 3),
        "diversions": sum(1 for a in A if a.diverted),
        "failures": len(fails), "failures_detected": len(det),
        "detection_rate": _round(len(det) / len(fails) if fails else None, 3),
        "detect_median_s": _round(median([f["detected_s"] for f in det]) if det else None, 1),
        "decide_median_s": _round(median([f["decided_s"] for f in det]) if det else None, 1),
        "assignments": len(A), "dispatches_initial": len(init),
    }
    return {"metrics": m, "incidents": rows, "failures": fails}


def tracks(sim: Sim, step: float = 60.0) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    end = min(sim.max_t, sim.cfg.horizon_s + 3600)
    for vid, st in sim.veh.items():
        pts, t = [], 0.0
        while t <= end:
            p = st.pos(t)
            pts.append([round(p[0], 5), round(p[1], 5)])
            t += step
        out[vid] = pts
    return out
