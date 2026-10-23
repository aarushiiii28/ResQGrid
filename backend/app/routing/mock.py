"""Deterministic offline router: a Manhattan-style street grid with detours around closures.
Same inputs always give the same route, so tests and simulations are reproducible."""

import itertools
from datetime import datetime

from app.routing.base import DisruptionSet, build_steps, finish_estimate
from app.routing.geometry import LatLon, from_xy, offset_point, polyline_length_m, to_xy
from app.schemas.routing import MatchResult, RouteEstimate

SPEED_MPS = 8.33  # ~30 km/h urban average
RING_BEARINGS = (0, 45, 90, 135, 180, 225, 270, 315)


def _nearest_on_segment(a: LatLon, b: LatLon, p: LatLon) -> tuple[LatLon, float]:
    """The point on segment ab closest to p, and its distance in metres (local flat projection around p)."""
    ax, ay = to_xy(a, p)
    bx, by = to_xy(b, p)
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return from_xy(qx, qy, p), (qx * qx + qy * qy) ** 0.5


def _legs(a: LatLon, b: LatLon) -> list[list[LatLon]]:
    """Two Manhattan variants between a and b: east-west first, or north-south first."""
    c1, c2 = (a[0], b[1]), (b[0], a[1])
    return [[a, c1, b], [a, c2, b]]


def _join(points: list[LatLon], variants: tuple[int, ...]) -> list[LatLon]:
    out: list[LatLon] = [points[0]]
    for i, v in enumerate(variants):
        leg = _legs(points[i], points[i + 1])[v]
        out += leg[1:]
    dedup = [out[0]]
    for p in out[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    return dedup


def _dedupe(routes: list[list[LatLon]]) -> list[list[LatLon]]:
    seen, out = set(), []
    for r in routes:
        key = tuple((round(p[0], 5), round(p[1], 5)) for p in r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


class MockRoutingProvider:
    name = "mock"

    async def alternatives(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                           now: datetime, k: int = 2) -> list[RouteEstimate]:
        """Deterministic alternatives to the primary route: the other Manhattan variant plus detours through a point
        offset to either side of the middle. Each is checked against closures (feasible=False if it still crosses one)."""
        primary = await self.route(origin, dest, disruptions, now)
        mid = ((origin[0] + dest[0]) / 2, (origin[1] + dest[1]) / 2)
        span = max(polyline_length_m([origin, dest]), 300.0)
        raw: list[list[LatLon]] = [_join([origin, dest], (1,))]
        for b in (90, 270, 0, 180):
            via = offset_point(mid, b, span * 0.35)
            raw.append(_join([origin, via, dest], (0, 0)))
            raw.append(_join([origin, via, dest], (1, 1)))
        have = [[(p[0], p[1]) for p in primary.polyline]]
        outs: list[RouteEstimate] = []
        for pl in _dedupe(raw):
            if any(len(pl) == len(h) and all(abs(a[0] - b[0]) < 1e-5 and abs(a[1] - b[1]) < 1e-5 for a, b in zip(pl, h, strict=True)) for h in have):
                continue
            steps, mid_s, slow_ids = build_steps(pl, SPEED_MPS, disruptions)
            hit = disruptions.hit_closures(pl)
            applied = [f"slow:{i}" for i in slow_ids] + [f"closure:{x.id}" for x in hit]
            outs.append(finish_estimate(
                pl=pl, steps=steps, mid_s=mid_s * (1.5 if hit else 1.0), source="mock", confidence=0.55, low_f=0.85,
                high_f=1.25 + 0.15 * len(slow_ids), now=now, applied=applied,
                notes=["alternative route"] + (["still crosses a closure"] if hit else []), feasible=not hit))
        outs.sort(key=lambda r: (not r.feasible, r.duration_s, r.distance_m))
        return outs[:k]

    async def match(self, points: list[LatLon], hints: list[list[LatLon]] | None = None,
                    radius_m: float = 25.0) -> MatchResult:
        """Deterministic stand-in for map matching: snap each fix onto the nearest known route (the committed route and its
        backups) when it is within radius_m, else leave it alone. A fix that is far from every known route is NOT dragged
        onto one: that is exactly how a wrong turn stays visible."""
        out, snapped = [], 0
        for p in points:
            best, best_d = None, radius_m
            for pl in hints or []:
                for a, b in zip(pl, pl[1:], strict=False):
                    q, d = _nearest_on_segment(a, b, p)
                    if d < best_d:
                        best, best_d = q, d
            if best is not None:
                out.append(best)
                snapped += 1
            else:
                out.append(p)
        return MatchResult(points=out, source="mock", confidence=0.5 if snapped else 0.3, snapped=snapped)

    async def route(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                    now: datetime) -> RouteEstimate:
        notes: list[str] = []
        base = _join([origin, dest], (0,))
        best = base
        hit_base = disruptions.hit_closures(base)
        applied = []
        feasible = True
        if hit_base:
            options: list[list[LatLon]] = []
            via_sets: list[list[LatLon]] = [[]]
            c = hit_base[0]
            ring_r = (c.radius_m or 0) * 1.6 + 30
            via_sets += [[offset_point((c.lat, c.lon), b, ring_r)] for b in RING_BEARINGS]
            for vias in via_sets:
                pts = [origin, *vias, dest]
                for variants in itertools.product((0, 1), repeat=len(pts) - 1):
                    options.append(_join(pts, variants))
            clear = [o for o in options if not disruptions.hit_closures(o)]
            if clear:
                best = min(clear, key=lambda p: (polyline_length_m(p), len(p)))
                applied = [f"closure:{x.id}" for x in hit_base]
                notes.append("rerouted around closure")
            else:
                best = min(options, key=lambda p: (len(disruptions.hit_closures(p)), polyline_length_m(p)))
                applied = [f"closure:{x.id}" for x in disruptions.hit_closures(best)]
                notes.append("no clear route; path crosses a closure")
                feasible = False

        steps, mid, slow_ids = build_steps(best, SPEED_MPS, disruptions)
        applied += [f"slow:{i}" for i in slow_ids]
        if not feasible:
            mid *= 1.5
        conf = 0.6 - (0.1 if slow_ids else 0) - (0.2 if not feasible else 0)
        return finish_estimate(
            pl=best, steps=steps, mid_s=mid, source="mock", confidence=conf, low_f=0.85,
            high_f=1.25 + 0.15 * len(slow_ids), now=now, applied=applied, notes=notes,
            feasible=feasible,
        )
