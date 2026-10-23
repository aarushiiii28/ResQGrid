"""M20 stable-route policy as pure functions: no I/O, no clock, no database. The live NavService and the Replay Lab
call exactly this code, so what the lab measures is what the driver gets.

Plan once and commit. Then: detect a confirmed wrong turn (deviation for N consecutive good fixes) and REJOIN the original
route first; reroute for congestion only when worth it (all of: ETA grew by pct and seconds, sustained for two evaluations,
the best alternative saves enough, cooldown over, not in the final approach); reroute at once if the road is blocked.
Every decision carries its reason codes and numbers. Prototype thresholds on a simulated city."""

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from app.core.geo import haversine_m
from app.routing.geometry import LatLon, bearing_deg, point_at, polyline_length_m, project_along, seg_point_dist_m

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "routing_policy.yaml"

Kind = Literal["keep", "rejoin", "reroute"]


def load_policy(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))


# ---- geometry on a committed route -------------------------------------------------------------------
def cross_track_m(pl: list[LatLon], p: LatLon) -> float:
    """Distance from a fix to the committed route polyline."""
    return min(seg_point_dist_m(pl[i], pl[i + 1], p) for i in range(len(pl) - 1))


def route_progress(pl: list[LatLon], p: LatLon) -> tuple[float, float]:
    """(distance along the route in metres, cross-track deviation in metres)."""
    return project_along(pl, p), cross_track_m(pl, p)


def heading_difference_deg(pl: list[LatLon], along_m: float, heading_deg: float | None) -> float | None:
    """Angle between the vehicle heading and the route direction at this point (0 = same way, 180 = opposite)."""
    if heading_deg is None:
        return None
    a, b = point_at(pl, max(0.0, along_m - 15)), point_at(pl, along_m + 15)
    if a == b:
        return None
    return abs((heading_deg - bearing_deg(a, b) + 540) % 360 - 180)


def heading_from_fixes(prev: LatLon | None, cur: LatLon) -> float | None:
    if prev is None or haversine_m(*prev, *cur) < 3:
        return None
    return bearing_deg(prev, cur)


def rejoin_candidates(pl: list[LatLon], along_m: float, max_ahead_m: float, step_m: float, max_n: int) -> list[tuple[float, LatLon]]:
    """Points on the ORIGINAL route ahead of the vehicle where it could rejoin, nearest first."""
    total = polyline_length_m(pl)
    out: list[tuple[float, LatLon]] = []
    d = along_m + step_m
    while d < min(total, along_m + max_ahead_m) and len(out) < max_n:
        out.append((d, point_at(pl, d)))
        d += step_m
    if not out or out[-1][0] < total - 1:
        if total - along_m <= max_ahead_m or not out:
            out.append((total, point_at(pl, total)))
    return out[:max_n]


# ---- state and observations ----------------------------------------------------------------------------
@dataclass
class TrackState:
    """Per-vehicle policy memory. Persisted by the service between evaluations."""

    version: int = 1
    reroutes: int = 0
    off_count: int = 0
    congestion_count: int = 0
    last_reroute_t: float | None = None
    last_heading: float | None = None
    history: list[dict] = field(default_factory=list)


@dataclass
class Obs:
    t: float                         # seconds (any monotonic clock)
    remaining_m: float
    cross_track_m: float
    accuracy_m: float | None
    remaining_eta_s: float           # ETA to go under the CURRENT conditions on the committed route
    planned_remaining_s: float       # what the plan promised for the same point
    blocked: bool                    # a closure lies on the remaining committed route
    off_confirmed: bool = False      # set by observe_fix when the deviation has lasted long enough


@dataclass
class Option:
    """A candidate way forward, already computed by the router."""

    eta_s: float                     # remaining time to the destination via this option
    blocked: bool = False            # it still crosses a closure
    label: str = ""
    rejoin_along_m: float | None = None
    payload: Any = None              # the route object, opaque to the policy


@dataclass
class Assessment:
    trigger: Literal["blocked", "off_route", "congestion", "uncertain", "none"]
    reasons: list[str]
    numbers: dict


@dataclass
class Decision:
    kind: Kind
    reasons: list[str]
    numbers: dict
    policy_version: str
    option: Option | None = None

    def as_log(self) -> dict:
        return {"decision": self.kind, "reason_codes": self.reasons, "numbers": self.numbers, "policy_version": self.policy_version}


def observe_fix(p: dict, s: TrackState, *, cross_track: float, accuracy_m: float | None, heading_deg: float | None = None) -> str:
    """Per-fix bookkeeping. Returns on_route | suspect | off_route | uncertain. A poor-accuracy fix changes nothing."""
    o = p["off_route"]
    if accuracy_m is not None and accuracy_m > o["max_accuracy_m"]:
        return "uncertain"
    if cross_track > o["deviation_m"]:
        s.off_count += 1
    else:
        s.off_count = 0
    s.last_heading = heading_deg if heading_deg is not None else s.last_heading
    if s.off_count >= o["consecutive_fixes"]:
        return "off_route"
    return "suspect" if s.off_count else "on_route"


def assess(p: dict, s: TrackState, o: Obs) -> Assessment:
    """Step 1 (cheap): does anything need the router? Updates the hysteresis counter."""
    c = p["congestion"]
    inc_s = o.remaining_eta_s - o.planned_remaining_s
    inc_pct = (inc_s / o.planned_remaining_s * 100.0) if o.planned_remaining_s > 0 else 0.0
    nums = {"remaining_m": round(o.remaining_m), "remaining_eta_s": round(o.remaining_eta_s, 1),
            "planned_remaining_s": round(o.planned_remaining_s, 1), "eta_increase_s": round(inc_s, 1),
            "eta_increase_pct": round(inc_pct, 1), "cross_track_m": round(o.cross_track_m, 1),
            "off_count": s.off_count, "congestion_count": s.congestion_count}
    if o.blocked:
        return Assessment("blocked", ["ROAD_BLOCKED"], nums)
    if o.off_confirmed:
        return Assessment("off_route", ["OFF_ROUTE_CONFIRMED"], nums)
    if o.accuracy_m is not None and o.accuracy_m > p["off_route"]["max_accuracy_m"]:
        return Assessment("uncertain", ["GPS_ACCURACY_POOR_NO_ACTION"], nums)
    worse = inc_pct > c["eta_increase_pct"] and inc_s > c["eta_increase_s"]
    if worse:
        s.congestion_count += 1
    else:
        s.congestion_count = 0
    nums["congestion_count"] = s.congestion_count
    if not worse:
        return Assessment("none", ["NO_MATERIAL_CHANGE"], nums)
    if s.congestion_count < c["sustained_evaluations"]:
        return Assessment("none", ["ETA_WORSE_NOT_SUSTAINED"], nums)
    return Assessment("congestion", ["ETA_WORSE_SUSTAINED"], nums)


def _best(options: list[Option], allow_blocked: bool = False) -> Option | None:
    ok = [x for x in options if allow_blocked or not x.blocked]
    return min(ok, key=lambda x: x.eta_s) if ok else None


def choose(p: dict, s: TrackState, o: Obs, a: Assessment, *, rejoin: list[Option] | None = None, replan: Option | None = None,
           alternatives: list[Option] | None = None) -> Decision:
    """Step 2: with the router's options, decide keep, rejoin or reroute, and say why in numbers."""
    ver = p["policy_version"]
    nums = dict(a.numbers)
    reasons = list(a.reasons)
    r = p["rejoin"]

    def pick_rejoin_or_replan(why: str) -> Decision:
        best_rejoin = _best(rejoin or [])
        full = replan if replan is not None and not replan.blocked else None
        if best_rejoin is not None:
            nums["rejoin_eta_s"] = round(best_rejoin.eta_s, 1)
            nums["rejoin_in_m"] = round((best_rejoin.rejoin_along_m or 0) - (o.remaining_m * 0))  # filled by the service for display
        if full is not None:
            nums["replan_eta_s"] = round(full.eta_s, 1)
        if best_rejoin is None and full is None:
            nums["note"] = "no usable route found"
            return Decision("keep", [*reasons, "NO_USABLE_ROUTE", why], nums, ver)
        if best_rejoin is not None and full is not None:
            margin = max(r["replan_margin_s"], best_rejoin.eta_s * r["replan_margin_pct"] / 100.0)
            if full.eta_s <= best_rejoin.eta_s - margin:
                nums["replan_margin_s"] = round(margin, 1)
                return Decision("reroute", [*reasons, "FULL_REPLAN_BEATS_REJOIN", why], nums, ver, full)
            return Decision("rejoin", [*reasons, "REJOIN_PREFERRED", why], nums, ver, best_rejoin)
        if best_rejoin is not None:
            return Decision("rejoin", [*reasons, "REJOIN_ONLY_OPTION", why], nums, ver, best_rejoin)
        return Decision("reroute", [*reasons, "REPLAN_ONLY_OPTION", why], nums, ver, full)

    if a.trigger == "blocked":
        d = pick_rejoin_or_replan("BLOCKED_REROUTE_NOW")
        if d.kind == "keep":
            d.reasons.append("BLOCKED_NO_CLEAR_ALTERNATIVE")
        return d
    if a.trigger == "off_route":
        return pick_rejoin_or_replan("WRONG_TURN_RECOVERY")
    if a.trigger == "congestion":
        c = p["congestion"]
        alt = _best(alternatives or ([replan] if replan else []))
        if alt is None:
            return Decision("keep", [*reasons, "NO_CLEAR_ALTERNATIVE"], nums, ver)
        saving = o.remaining_eta_s - alt.eta_s
        save_pct = saving / o.remaining_eta_s * 100.0 if o.remaining_eta_s > 0 else 0.0
        nums.update(alt_eta_s=round(alt.eta_s, 1), saving_s=round(saving, 1), saving_pct=round(save_pct, 1))
        fails = []
        if saving < c["min_saving_s"] or save_pct < c["min_saving_pct"]:
            fails.append("SAVING_TOO_SMALL")
        if s.last_reroute_t is not None and o.t - s.last_reroute_t < c["cooldown_s"]:
            nums["since_last_reroute_s"] = round(o.t - s.last_reroute_t, 1)
            fails.append("COOLDOWN_ACTIVE")
        if o.remaining_m <= c["final_approach_m"]:
            fails.append("FINAL_APPROACH_LOCKED")
        if fails:
            return Decision("keep", [*reasons, *fails], nums, ver)
        return Decision("reroute", [*reasons, "WORTH_IT_SAVES_ENOUGH"], nums, ver, alt)
    # none / uncertain
    return Decision("keep", reasons, nums, ver)


async def build_rejoin_options(p: dict, route_fn, here: LatLon, pl: list[LatLon], along_m: float, disr, scale: float,
                               speed_mps: float) -> list[Option]:
    """Router-backed rejoin options: for each candidate point ahead on the ORIGINAL route, the time to get there plus the time left
    along the original route. `route_fn(a, b, disr)` returns an object with .duration_s and .polyline. Shared by the live
    service and the Replay Lab so both measure the same thing."""
    from app.routing.base import build_steps
    from app.routing.geometry import tail_from

    r = p["rejoin"]
    out: list[Option] = []
    for ra, pt in rejoin_candidates(pl, along_m, r["max_ahead_m"], r["step_m"], r["max_candidates"]):
        conn = await route_fn(here, pt, disr)
        tail2 = tail_from(pl, ra)
        _, tail_dur, _ = build_steps(tail2, speed_mps, disr)
        blocked = bool(disr.hit_closures([(q[0], q[1]) for q in conn.polyline])) or bool(disr.hit_closures(tail2))
        out.append(Option(eta_s=conn.duration_s + tail_dur * scale, blocked=blocked, label=f"rejoin {round(ra - along_m)} m ahead",
                          rejoin_along_m=ra, payload=(conn, ra, tail2, tail_dur * scale)))
    return out


def apply_decision(s: TrackState, d: Decision, t: float) -> None:
    """Bookkeeping after a decision was acted on."""
    s.history = [*s.history, {"t": round(t, 1), **d.as_log()}][-40:]
    if d.kind in ("rejoin", "reroute"):
        s.version += 1
        s.reroutes += 1
        s.last_reroute_t = t
        s.off_count = 0
        s.congestion_count = 0


def mean_eta_error(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))
