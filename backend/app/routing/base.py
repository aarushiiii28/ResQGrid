from datetime import datetime
from typing import Protocol

from app.routing.geometry import (
    LatLon,
    bearing_deg,
    compass,
    densify,
    path_hits_circle,
    polyline_length_m,
)
from app.schemas.routing import Disruption, MatchResult, RouteEstimate, RouteStep


class DisruptionSet:
    """Active disruptions injected into routing: closures, congestion, event zones."""

    def __init__(self, items: list[Disruption] | None = None):
        self.items = items or []

    @property
    def closures(self) -> list[Disruption]:
        return [d for d in self.items if d.kind == "closure" and d.lat is not None]

    @property
    def slow_zones(self) -> list[Disruption]:
        return [d for d in self.items if d.kind in ("congestion", "event_zone")]

    @property
    def blocked_entrances(self) -> set[str]:
        return {d.entrance_id for d in self.items if d.kind == "blocked_entrance" and d.entrance_id}

    def hit_closures(self, pl: list[LatLon]) -> list[Disruption]:
        return [c for c in self.closures if path_hits_circle(pl, (c.lat, c.lon), c.radius_m or 0)]

    def segment_multiplier(self, a: LatLon, b: LatLon) -> tuple[float, list[str]]:
        m, ids = 1.0, []
        for z in self.slow_zones:
            if z.lat is None or path_hits_circle([a, b], (z.lat, z.lon), z.radius_m or 0):
                m *= z.multiplier
                ids.append(z.id)
        return m, ids


class RoutingProvider(Protocol):
    name: str

    async def route(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                    now: datetime) -> RouteEstimate: ...

    async def alternatives(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                           now: datetime, k: int = 2) -> list[RouteEstimate]: ...

    async def match(self, points: list[LatLon], hints: list[list[LatLon]] | None = None,
                    radius_m: float = 25.0) -> MatchResult: ...


def turn_instruction(prev_bearing: float, new_bearing: float) -> tuple[str, str]:
    d = (new_bearing - prev_bearing + 540) % 360 - 180  # -180..180, + = right
    a = abs(d)
    if a < 20:
        return "continue", "Continue straight"
    if a > 160:
        return "uturn", "Make a U-turn"
    side = "right" if d > 0 else "left"
    if a < 60:
        return f"turn-{side}", f"Bear {side}"
    return f"turn-{side}", f"Turn {side}"


def build_steps(pl: list[LatLon], speed_mps: float, disruptions: DisruptionSet) -> tuple[
    list[RouteStep], float, list[str]
]:
    """Steps and total (mid) duration for a polyline using a flat speed, with slow-zone multipliers."""
    steps: list[RouteStep] = []
    total, applied = 0.0, []
    prev_b: float | None = None
    for i in range(len(pl) - 1):
        a, b = pl[i], pl[i + 1]
        from app.core.geo import haversine_m

        dist = haversine_m(*a, *b)
        if dist < 1:
            continue
        mult, ids = disruptions.segment_multiplier(a, b)
        applied += [x for x in ids if x not in applied]
        dur = dist / speed_mps * mult
        total += dur
        brg = bearing_deg(a, b)
        if prev_b is None:
            man, text = "depart", f"Head {compass(brg)}"
        else:
            man, text = turn_instruction(prev_b, brg)
        if man == "continue" and steps:
            last = steps[-1]
            steps[-1] = last.model_copy(update={"distance_m": last.distance_m + dist,
                                                "duration_s": last.duration_s + dur})
        else:
            steps.append(RouteStep(instruction=text, maneuver=man, distance_m=dist, duration_s=dur,
                                   lat=a[0], lon=a[1]))
        prev_b = brg
    end = pl[-1]
    steps.append(RouteStep(instruction="Arrive at the destination", maneuver="arrive", distance_m=0,
                           duration_s=0, lat=end[0], lon=end[1]))
    return steps, total, applied


def finish_estimate(*, pl: list[LatLon], steps: list[RouteStep], mid_s: float, source: str,
                    confidence: float, low_f: float, high_f: float, now: datetime,
                    applied: list[str], notes: list[str], degraded: bool = False,
                    feasible: bool = True) -> RouteEstimate:
    return RouteEstimate(
        distance_m=round(polyline_length_m(pl), 1),
        duration_low_s=round(mid_s * low_f, 1), duration_s=round(mid_s, 1),
        duration_high_s=round(mid_s * high_f, 1),
        polyline=densify(pl), steps=steps, source=source,  # type: ignore[arg-type]
        confidence=round(confidence, 2), degraded=degraded, computed_at=now,
        applied_disruptions=applied, notes=notes, feasible=feasible,
    )
