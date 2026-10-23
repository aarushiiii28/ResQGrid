from datetime import UTC, datetime, timedelta

from app.schemas.common import GeoPoint
from app.schemas.vehicle import TRANSITIONS, Vehicle
from app.services.vehicle_service import eligibility, make_view

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
LOC = GeoPoint(lat=30.9, lon=75.85)


def veh(tags=("BLS",), status="available", age_s=5, offline=False, loc=LOC):
    t = NOW - timedelta(seconds=age_s)
    return Vehicle(id="AMB-X", capability_tags=list(tags), status=status, location=loc,
                   location_at=t, last_update_at=t, offline=offline)


def test_eligible_when_available_fresh_and_capable():
    ok, why = eligibility(veh(("ALS", "cardiac")), {"ALS", "cardiac"}, NOW, 90)
    assert ok and why == []


def test_als_implies_bls():
    assert eligibility(veh(("ALS",)), {"BLS"}, NOW, 90)[0]


def test_bls_cannot_serve_als():
    ok, why = eligibility(veh(("BLS",)), {"ALS"}, NOW, 90)
    assert not ok and "missing_capability:ALS" in why


def test_unavailable_and_assigned_excluded():
    for st in ("unavailable", "assigned", "en_route", "transporting"):
        ok, why = eligibility(veh(status=st), {"BLS"}, NOW, 90)
        assert not ok and f"status_{st}" in why


def test_stale_available_becomes_unknown_not_eligible():
    v = veh(age_s=300)
    view = make_view(v, NOW, 90)
    assert view.availability == "unknown"
    assert view.location_state == "stale"
    assert view.location_age_s == 300
    ok, why = eligibility(v, {"BLS"}, NOW, 90)
    assert not ok and "availability_unknown_stale" in why


def test_offline_vehicle_is_unknown_and_excluded():
    v = veh(offline=True)
    assert make_view(v, NOW, 90).availability == "unknown"
    assert "offline" in eligibility(v, {"BLS"}, NOW, 90)[1]


def test_no_location_never_live():
    v = veh(loc=None)
    v.location_at = None
    assert make_view(v, NOW, 90).location_state == "unknown"
    assert "no_location" in eligibility(v, {"BLS"}, NOW, 90)[1]


def test_transition_table_is_closed_and_sane():
    assert "en_route" not in TRANSITIONS["available"]
    assert "transporting" not in TRANSITIONS["assigned"]
    assert TRANSITIONS["unavailable"] == {"available"}
    for dst in TRANSITIONS.values():
        assert dst <= set(TRANSITIONS)
