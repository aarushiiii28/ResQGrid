"""OSRM adapter (public demo endpoint is not a production guarantee). On any failure it falls back
to the deterministic mock router and labels the result degraded; it never invents live data."""

from datetime import datetime

import httpx

from app.routing.base import DisruptionSet, finish_estimate
from app.routing.geometry import LatLon, offset_point
from app.routing.mock import RING_BEARINGS, MockRoutingProvider
from app.schemas.routing import MatchResult, RouteEstimate, RouteStep


def _instruction(man_type: str, modifier: str | None, name: str | None) -> tuple[str, str]:
    mod = (modifier or "").replace("slight ", "bear ").replace("sharp ", "sharp ")
    on = f" onto {name}" if name else ""
    if man_type == "depart":
        return "depart", f"Head out{(' on ' + name) if name else ''}"
    if man_type == "arrive":
        return "arrive", "Arrive at the destination"
    if man_type in ("turn", "end of road", "fork", "on ramp", "off ramp") and modifier:
        side = "left" if "left" in modifier else "right" if "right" in modifier else "straight"
        if side == "straight":
            return "continue", f"Continue straight{on}"
        return f"turn-{side}", (f"Bear {side}" if "slight" in modifier else f"Turn {side}") + on
    if man_type == "roundabout" or man_type == "rotary":
        return "roundabout", f"Enter the roundabout{on}"
    if modifier == "uturn":
        return "uturn", "Make a U-turn"
    return "continue", f"Continue{on}" if not mod else f"{mod.capitalize()}{on}"


class OSRMProvider:
    name = "osrm"

    def __init__(self, base_url: str, timeout_s: float = 3.0,
                 client: httpx.AsyncClient | None = None, fallback: MockRoutingProvider | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self.fallback = fallback or MockRoutingProvider()

    async def _fetch(self, pts: list[LatLon]) -> dict:
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pts)
        url = f"{self.base_url}/route/v1/driving/{coords}"
        params = {"overview": "full", "geometries": "geojson", "steps": "true"}
        if self._client is not None:
            r = await self._client.get(url, params=params, timeout=self.timeout_s)
        else:
            async with httpx.AsyncClient() as c:
                r = await c.get(url, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM code={data.get('code')}")
        return data["routes"][0]

    async def _get(self, url: str, params: dict) -> dict:
        if self._client is not None:
            r = await self._client.get(url, params=params, timeout=self.timeout_s)
        else:
            async with httpx.AsyncClient() as c:
                r = await c.get(url, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def _estimate(self, route: dict, disruptions: DisruptionSet, now: datetime, notes: list[str]) -> RouteEstimate:
        pl = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]
        applied: list[str] = []
        hits = disruptions.hit_closures(pl)
        steps: list[RouteStep] = []
        for leg in route["legs"]:
            for s in leg["steps"]:
                m = s["maneuver"]
                man, text = _instruction(m["type"], m.get("modifier"), s.get("name") or None)
                steps.append(RouteStep(instruction=text, maneuver=man, distance_m=s["distance"], duration_s=s["duration"],
                                       lat=m["location"][1], lon=m["location"][0], name=s.get("name") or None))
        extra = 1.0
        for a, b in zip(pl, pl[1:], strict=False):
            mult, ids = disruptions.segment_multiplier(a, b)
            if mult > 1:
                extra = max(extra, mult)
                applied += [f"slow:{i}" for i in ids if f"slow:{i}" not in applied]
        applied += [f"closure:{x.id}" for x in hits]
        return finish_estimate(pl=pl, steps=steps, mid_s=float(route["duration"]) * extra, source="osrm",
                               confidence=0.7 - (0.1 if extra > 1 else 0), low_f=0.9, high_f=1.25 + (0.15 if extra > 1 else 0),
                               now=now, applied=applied, notes=notes, feasible=not hits)

    async def alternatives(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                           now: datetime, k: int = 2) -> list[RouteEstimate]:
        """Other routes OSRM offers for the same trip (alternatives=true), each checked against active closures."""
        try:
            coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in (origin, dest))
            data = await self._get(f"{self.base_url}/route/v1/driving/{coords}",
                                   {"overview": "full", "geometries": "geojson", "steps": "true", "alternatives": "true"})
            if data.get("code") != "Ok":
                raise RuntimeError(f"OSRM code={data.get('code')}")
            outs = [self._estimate(r, disruptions, now, ["alternative route"]) for r in data.get("routes", [])[1 : k + 1]]
            outs.sort(key=lambda r: (not r.feasible, r.duration_s))
            return outs
        except Exception as e:
            outs = await self.fallback.alternatives(origin, dest, disruptions, now, k)
            return [o.model_copy(update={"source": "mock_fallback", "degraded": True, "confidence": min(o.confidence, 0.4),
                                         "notes": [*o.notes, f"degraded: OSRM unavailable ({type(e).__name__}); mock routing used"]}) for o in outs]

    async def match(self, points: list[LatLon], hints: list[list[LatLon]] | None = None,
                    radius_m: float = 25.0) -> MatchResult:
        """Snap a short GPS trace onto the road network with OSRM /match. Falls back to the deterministic matcher."""
        try:
            if len(points) < 2:  # OSRM needs at least two points; a single fix is matched against known routes only
                raise ValueError("need two points")
            coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
            data = await self._get(f"{self.base_url}/match/v1/driving/{coords}",
                                   {"overview": "false", "radiuses": ";".join([str(int(radius_m))] * len(points))})
            if data.get("code") != "Ok":
                raise RuntimeError(f"OSRM code={data.get('code')}")
            tps = data.get("tracepoints", [])
            out, snapped = [], 0
            for p, tp in zip(points, tps, strict=False):
                if tp and tp.get("location"):
                    out.append((tp["location"][1], tp["location"][0]))
                    snapped += 1
                else:
                    out.append(p)
            conf = sum((m.get("confidence", 0.5) for m in data.get("matchings", [])), 0.0) / max(1, len(data.get("matchings", [])))
            return MatchResult(points=out, source="osrm", confidence=round(min(1.0, conf), 2), snapped=snapped)
        except Exception:
            m = await self.fallback.match(points, hints, radius_m)
            return m.model_copy(update={"source": "mock_fallback", "degraded": True, "confidence": min(m.confidence, 0.4)})

    async def route(self, origin: LatLon, dest: LatLon, disruptions: DisruptionSet,
                    now: datetime) -> RouteEstimate:
        try:
            route = await self._fetch([origin, dest])
            pl = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]
            notes: list[str] = []
            applied: list[str] = []
            hits = disruptions.hit_closures(pl)
            if hits:  # public OSRM has no closure input: force a via-point and re-query
                c = hits[0]
                ring = (c.radius_m or 0) * 1.6 + 30
                best = None
                for b in RING_BEARINGS:
                    via = offset_point((c.lat, c.lon), b, ring)
                    try:
                        cand = await self._fetch([origin, via, dest])
                    except Exception:
                        continue
                    cpl = [(x[1], x[0]) for x in cand["geometry"]["coordinates"]]
                    if not disruptions.hit_closures(cpl) and (best is None or cand["duration"] < best[0]["duration"]):
                        best = (cand, cpl)
                if best:
                    route, pl = best
                    applied = [f"closure:{x.id}" for x in hits]
                    notes.append("rerouted around closure via waypoint")
                else:
                    notes.append("closure on path; no clear alternative found")
            steps: list[RouteStep] = []
            for leg in route["legs"]:
                for s in leg["steps"]:
                    m = s["maneuver"]
                    man, text = _instruction(m["type"], m.get("modifier"), s.get("name") or None)
                    steps.append(RouteStep(instruction=text, maneuver=man, distance_m=s["distance"],
                                           duration_s=s["duration"], lat=m["location"][1],
                                           lon=m["location"][0], name=s.get("name") or None))
            # OSRM has no live traffic: apply injected congestion as multipliers
            mid = float(route["duration"])
            extra = 1.0
            for a, b in zip(pl, pl[1:], strict=False):
                m, ids = disruptions.segment_multiplier(a, b)
                if m > 1:
                    extra = max(extra, m)
                    applied += [f"slow:{i}" for i in ids if f"slow:{i}" not in applied]
            mid *= extra
            return finish_estimate(
                pl=pl, steps=steps, mid_s=mid, source="osrm", confidence=0.75 - (0.1 if extra > 1 else 0),
                low_f=0.9, high_f=1.25 + (0.15 if extra > 1 else 0), now=now, applied=applied,
                notes=notes, feasible=not (hits and not applied),
            )
        except Exception as e:
            est = await self.fallback.route(origin, dest, disruptions, now)
            return est.model_copy(update={
                "source": "mock_fallback", "degraded": True,
                "confidence": min(est.confidence, 0.4),
                "notes": [*est.notes, f"degraded: OSRM unavailable ({type(e).__name__}); mock routing used"],
            })
