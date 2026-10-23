import asyncio
from datetime import UTC, datetime

import httpx

from app.routing.base import DisruptionSet
from app.routing.geometry import path_hits_circle
from app.routing.mock import MockRoutingProvider
from app.routing.osrm import OSRMProvider
from app.schemas.routing import Disruption

NOW = datetime(2026, 1, 1, tzinfo=UTC)
A, B = (30.90, 75.85), (30.93, 75.89)


def run(coro):
    return asyncio.run(coro)


def closure(lat, lon, r=150):
    return Disruption(id="D1", kind="closure", lat=lat, lon=lon, radius_m=r, created_at=NOW)


# ---- deterministic mock ---------------------------------------------------------------------------------------------
def test_mock_alternatives_are_distinct_deterministic_and_at_most_k():
    m = MockRoutingProvider()
    primary = run(m.route(A, B, DisruptionSet(), NOW))
    alts = run(m.alternatives(A, B, DisruptionSet(), NOW, 2))
    again = run(m.alternatives(A, B, DisruptionSet(), NOW, 2))
    assert 1 <= len(alts) <= 2 and [a.polyline for a in alts] == [a.polyline for a in again]
    assert all(a.polyline != primary.polyline for a in alts)
    assert alts[0].polyline != alts[-1].polyline or len(alts) == 1
    assert all(a.feasible and a.source == "mock" and a.notes[0] == "alternative route" for a in alts)
    assert len(run(m.alternatives(A, B, DisruptionSet(), NOW, 1))) == 1


def test_mock_alternatives_flag_routes_that_still_cross_a_closure_and_sort_feasible_first():
    m = MockRoutingProvider()
    primary = run(m.route(A, B, DisruptionSet(), NOW))
    mid = primary.polyline[len(primary.polyline) // 2]
    ds = DisruptionSet([closure(mid[0], mid[1], 200)])
    alts = run(m.alternatives(A, B, ds, NOW, 2))
    assert alts and all(a.feasible == (not path_hits_circle(a.polyline, mid, 200)) for a in alts)
    feas = [a.feasible for a in alts]
    assert feas == sorted(feas, reverse=True)


def test_mock_matcher_snaps_close_fixes_onto_known_routes_and_leaves_a_wrong_turn_alone():
    m = MockRoutingProvider()
    route = run(m.route(A, B, DisruptionSet(), NOW))
    pl = route.polyline
    near = (pl[3][0] + 0.00005, pl[3][1])                 # ~5 m off the route
    far = (pl[3][0] + 0.003, pl[3][1])                    # ~330 m off
    out = run(m.match([near, far], [pl], 25.0))
    assert out.source == "mock" and out.snapped == 1
    assert out.points[0] != near and path_hits_circle(pl, out.points[0], 1.0)      # snapped onto the polyline
    assert out.points[1] == far                                                       # NOT dragged back: the wrong turn stays visible
    assert run(m.match([near], None)).points == [near]


# ---- OSRM with a fake HTTP server --------------------------------------------------------------------------------------------
def osrm_route(shift=0.0, dur=300.0):
    coords = [[75.85, 30.90 + shift], [75.87, 30.90 + shift], [75.87, 30.91 + shift]]
    return {"duration": dur, "distance": 3000, "geometry": {"coordinates": coords},
            "legs": [{"steps": [{"maneuver": {"type": "depart", "location": coords[0]}, "distance": 2000, "duration": 200, "name": "Ring Road"},
                                {"maneuver": {"type": "arrive", "location": coords[-1]}, "distance": 0, "duration": 0, "name": ""}]}]}


def osrm(handler):
    return OSRMProvider("http://osrm.test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_osrm_alternatives_use_alternatives_true_and_check_closures():
    seen = {}

    def handler(req: httpx.Request):
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, json={"code": "Ok", "routes": [osrm_route(0, 300), osrm_route(0.001, 330), osrm_route(0.002, 360), osrm_route(0.003, 390)]})

    p = osrm(handler)
    alts = run(p.alternatives(A, B, DisruptionSet(), NOW, 2))
    assert seen["q"]["alternatives"] == "true" and len(alts) == 2
    assert [a.duration_s for a in alts] == [330.0, 360.0] and all(a.source == "osrm" and a.feasible for a in alts)
    ds = DisruptionSet([closure(30.9015, 75.86, 300)])
    blocked = run(osrm(handler).alternatives(A, B, ds, NOW, 2))
    assert any(not a.feasible for a in blocked)


def test_osrm_match_snaps_tracepoints_and_keeps_unmatched_fixes():
    def handler(req: httpx.Request):
        assert "/match/v1/driving/" in req.url.path and req.url.params["radiuses"] == "25;25;25"
        return httpx.Response(200, json={"code": "Ok", "matchings": [{"confidence": 0.8}],
                                         "tracepoints": [{"location": [75.8501, 30.9001]}, None, {"location": [75.8503, 30.9003]}]})

    pts = [(30.90005, 75.85005), (30.9002, 75.8502), (30.90035, 75.85035)]
    out = run(osrm(handler).match(pts, None, 25.0))
    assert out.source == "osrm" and out.snapped == 2 and out.confidence == 0.8
    assert out.points[0] == (30.9001, 75.8501) and out.points[1] == pts[1]


def test_osrm_failures_fall_back_to_the_mock_and_say_degraded():
    def down(req):
        raise httpx.ConnectError("no route to host")

    p = osrm(down)
    alts = run(p.alternatives(A, B, DisruptionSet(), NOW, 2))
    assert alts and all(a.source == "mock_fallback" and a.degraded and "OSRM unavailable" in a.notes[-1] for a in alts)
    m = run(p.match([A, B], [[A, B]], 25.0))
    assert m.source == "mock_fallback" and m.degraded and m.confidence <= 0.4
    one = run(osrm(lambda r: httpx.Response(200, json={"code": "Ok", "routes": [], "tracepoints": []})).match([A], None, 25.0))
    assert one.degraded                                                                # OSRM /match needs two points: use the fallback
    bad = run(osrm(lambda r: httpx.Response(200, json={"code": "NoMatch"})).match([A, B], None, 25.0))
    assert bad.degraded
