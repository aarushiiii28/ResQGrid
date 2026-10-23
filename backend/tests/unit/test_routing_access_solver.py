import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.optimization.ortools_solver import greedy, ortools_assign
from app.optimization.scoring import PairInputs, score_pair, weights_from
from app.routing.base import DisruptionSet
from app.routing.geometry import path_hits_circle
from app.routing.mock import MockRoutingProvider
from app.routing.osrm import OSRMProvider
from app.schemas.access import Building, Entrance, TempAccessReport
from app.schemas.common import GeoPoint
from app.schemas.routing import Disruption
from app.services.access_service import AccessService

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
A, B = (30.90, 75.85), (30.92, 75.88)


def run(coro):
    return asyncio.run(coro)


def dis(**kw):
    base = dict(id="DIS-1", kind="closure", created_at=NOW)
    return Disruption(**{**base, **kw})


# ---- routing ------------------------------------------------------------
def test_mock_route_is_deterministic_with_range_source_steps():
    p = MockRoutingProvider()
    r1 = run(p.route(A, B, DisruptionSet(), NOW))
    r2 = run(p.route(A, B, DisruptionSet(), NOW))
    assert r1 == r2
    assert r1.duration_low_s < r1.duration_s < r1.duration_high_s
    assert r1.source == "mock" and 0 < r1.confidence < 1 and not r1.degraded
    assert r1.steps[0].maneuver == "depart" and r1.steps[-1].maneuver == "arrive"
    assert r1.polyline[0] == A and r1.polyline[-1] == B
    assert abs(sum(s.distance_m for s in r1.steps) - r1.distance_m) < 1


def test_closure_on_one_variant_swaps_to_equal_length_alternative():
    p = MockRoutingProvider()
    base = run(p.route(A, B, DisruptionSet(), NOW))
    corner = (A[0], B[1])  # base route goes via this corner
    assert corner in base.polyline
    c = dis(lat=corner[0], lon=corner[1], radius_m=400)
    r = run(p.route(A, B, DisruptionSet([c]), NOW))
    assert r.polyline != base.polyline and not path_hits_circle(r.polyline, corner, 400)
    assert "closure:DIS-1" in r.applied_disruptions and r.feasible


def test_closure_on_straight_road_forces_longer_detour():
    p = MockRoutingProvider()
    s, e = (30.90, 75.85), (30.94, 75.85)  # one straight north-south road: no equal-length alternative
    base = run(p.route(s, e, DisruptionSet(), NOW))
    mid = (30.92, 75.85)
    c = dis(lat=mid[0], lon=mid[1], radius_m=400)
    r = run(p.route(s, e, DisruptionSet([c]), NOW))
    assert r.feasible and not path_hits_circle(r.polyline, mid, 400)
    assert r.distance_m > base.distance_m and r.duration_s > base.duration_s
    assert any(st.maneuver.startswith("turn") for st in r.steps)

def test_congestion_multiplier_slows_and_widens_range():
    p = MockRoutingProvider()
    base = run(p.route(A, B, DisruptionSet(), NOW))
    slow = run(p.route(A, B, DisruptionSet([dis(id="DIS-2", kind="congestion", multiplier=2.0)]), NOW))
    assert slow.duration_s == pytest.approx(base.duration_s * 2, rel=0.01)
    assert (slow.duration_high_s - slow.duration_low_s) > (base.duration_high_s - base.duration_low_s)
    assert slow.confidence < base.confidence


def osrm_payload(coords):
    return {"code": "Ok", "routes": [{
        "duration": 600.0, "distance": 5000.0,
        "geometry": {"coordinates": [[lo, la] for la, lo in coords]},
        "legs": [{"steps": [
            {"distance": 5000.0, "duration": 600.0, "name": "Test Rd",
             "maneuver": {"type": "depart", "location": [coords[0][1], coords[0][0]]}},
            {"distance": 0.0, "duration": 0.0, "name": "",
             "maneuver": {"type": "arrive", "location": [coords[-1][1], coords[-1][0]]}}]}],
    }]}


def test_osrm_parses_route_and_steps():
    def handler(req):
        return httpx.Response(200, json=osrm_payload([A, B]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = run(OSRMProvider("http://osrm.test", client=client).route(A, B, DisruptionSet(), NOW))
    assert r.source == "osrm" and not r.degraded and r.duration_s == 600
    assert r.steps[0].name == "Test Rd" and r.steps[-1].maneuver == "arrive"


def test_osrm_failure_falls_back_to_labelled_mock():
    def boom(req):
        raise httpx.ConnectTimeout("timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    r = run(OSRMProvider("http://osrm.test", client=client).route(A, B, DisruptionSet(), NOW))
    assert r.source == "mock_fallback" and r.degraded
    assert any("degraded" in n for n in r.notes) and r.confidence <= 0.4 and r.steps


def test_osrm_bad_status_falls_back():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    r = run(OSRMProvider("http://osrm.test", client=client).route(A, B, DisruptionSet(), NOW))
    assert r.degraded and r.source == "mock_fallback"


def test_osrm_reroutes_via_waypoint_when_closure_hits():
    calls = []

    def handler(req):
        n = req.url.path.count(";")
        calls.append(n)
        if n == 1:  # direct route runs through the closure
            return httpx.Response(200, json=osrm_payload([A, (30.91, 75.865), B]))
        return httpx.Response(200, json=osrm_payload([A, (30.925, 75.85), B]))  # via-point route

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = dis(lat=30.91, lon=75.865, radius_m=300)
    r = run(OSRMProvider("http://osrm.test", client=client).route(A, B, DisruptionSet([c]), NOW))
    assert 2 in calls and "closure:DIS-1" in r.applied_disruptions


# ---- M2 entrances ---------------------------------------------------------
def building(entrances, reports=()):
    return Building(id="B1", name="Block A", centroid=GeoPoint(lat=30.9, lon=75.85),
                    entrances=entrances, temp_reports=list(reports))


def ent(id_, conf=0.9, **kw):
    kw.setdefault("verified_at", NOW - timedelta(days=5))
    return Entrance(id=id_, name=f"Gate {id_}", kind=kw.pop("kind", "side"),
                    location=GeoPoint(lat=30.9, lon=75.85), confidence=conf, **kw)


def svc():
    return AccessService(None, None, None, Settings(), lambda: NOW)


def report(eid, kind, expires_in_s):
    return TempAccessReport(id=f"R-{eid}-{kind}", entrance_id=eid, kind=kind,
                            reported_at=NOW - timedelta(minutes=5), expires_at=NOW + timedelta(seconds=expires_in_s))


def test_clear_best_entrance_needs_no_confirmation():
    d = svc().decide(building([ent("E1", 0.95, kind="emergency"), ent("E2", 0.6)]))
    assert d.chosen.entrance.id == "E1" and not d.needs_confirmation


def test_uncertain_entrance_returns_alternatives_and_confirmation():
    d = svc().decide(building([ent("E1", 0.62), ent("E2", 0.6)]))
    assert d.needs_confirmation and d.confirmation_request and len(d.alternatives) == 1


def test_temp_blocked_report_excludes_then_expires():
    es = [ent("E1", 0.95), ent("E2", 0.7)]
    live = svc().decide(building(es, [report("E1", "blocked", 600)]))
    assert live.chosen.entrance.id == "E2"
    expired = svc().decide(building(es, [report("E1", "blocked", -1)]))
    assert expired.chosen.entrance.id == "E1" and expired.expired_report_ids == ["R-E1-blocked"]


def test_blocked_entrance_disruption_and_all_blocked():
    es = [ent("E1", 0.95), ent("E2", 0.9)]
    assert svc().decide(building(es), {"E1"}).chosen.entrance.id == "E2"
    d = svc().decide(building(es), {"E1", "E2"})
    assert d.chosen is None and d.needs_confirmation


# ---- scoring + solver --------------------------------------------------------
def inputs(**kw):
    base = dict(scene_mid_s=300, hospital_mid_s=600, total_low_s=720, total_high_s=1100,
                vehicle_freshness=0.1, hospital_freshness=0.0, coverage_loss=0.03)
    return PairInputs(**{**base, **kw})


def test_score_components_sum_and_follow_weights():
    w = weights_from(Settings())
    c = score_pair(inputs(), w)
    assert c.total == pytest.approx(sum(c.weighted.values()), abs=1e-3)
    assert c.time == pytest.approx(5 + 0.5 * 10)
    w2 = w.model_copy(update={"w_coverage": 0})
    assert score_pair(inputs(), w2).total < c.total


def test_switch_penalty_raises_score():
    w = weights_from(Settings())
    stay, move = score_pair(inputs(switch=0), w), score_pair(inputs(switch=1.5), w)
    assert move.total - stay.total == pytest.approx(w.w_switch * 1.5)


def test_stale_hospital_scores_worse_than_fresh():
    w = weights_from(Settings())
    assert score_pair(inputs(hospital_freshness=1.0), w).total > score_pair(inputs(hospital_freshness=0.0), w).total


def test_ortools_beats_greedy_on_fixed_instance():
    cost = {("A", "v1"): 1, ("A", "v2"): 2, ("B", "v1"): 2, ("B", "v2"): 100}
    g, o = greedy(["A", "B"], cost), ortools_assign(["A", "B"], cost)
    assert g.total_cost == 101 and o.total_cost == 4
    assert o.assignment == {"A": "v2", "B": "v1"} and o.status == "OPTIMAL"


def test_ortools_never_worse_than_greedy_and_serves_at_least_as_many():
    import random

    rng = random.Random(7)
    wins = 0
    for _ in range(25):
        incs = [f"I{i}" for i in range(rng.randint(2, 6))]
        vs = [f"V{i}" for i in range(rng.randint(2, 7))]
        cost = {(i, v): round(rng.uniform(2, 30), 2) for i in incs for v in vs if rng.random() < 0.8}
        g, o = greedy(incs, cost), ortools_assign(incs, cost)
        assert len(o.assignment) >= len(g.assignment)
        if len(o.assignment) == len(g.assignment):
            assert o.total_cost <= g.total_cost + 1e-6
            wins += o.total_cost < g.total_cost - 1e-6
    assert wins >= 1  # the optimizer finds strictly better plans on some instances


def test_ortools_is_deterministic():
    cost = {(f"I{i}", f"V{j}"): float((i * 7 + j * 3) % 11 + 1) for i in range(5) for j in range(6)}
    runs = [ortools_assign([f"I{i}" for i in range(5)], cost) for _ in range(3)]
    assert all(r.assignment == runs[0].assignment for r in runs)
