import pytest

from app.routing.geometry import haversine_m, path_hits_circle, point_at
from app.services import routing_policy as rp
from tests.conftest import token_for
from tests.integration.test_driver_hospital_flow import DISP, drv, make_assigned, set_status


@pytest.fixture
def trip(client):
    """An assigned, accepted, en-route unit with the stable-route policy evaluating on every fix."""
    ctx = client.app.state.ctx
    ctx.stability.policy["evaluation"]["interval_s"] = 0
    iid, vid, hid, _ = make_assigned(client)
    a = client.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    client.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    set_status(client, vid, "en_route", 1)
    client.get(f"/api/driver/{vid}/plan", headers=drv(vid))
    return client, ctx, iid, vid, hid


def plan(c, vid):
    return c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()


def move(c, vid, seconds):
    return c.portal.call(lambda: c.app.state.ctx.nav.advance(vid, seconds))


def decisions(c, iid):
    return [e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json() if e["event_type"] == "route.decision"]


def drive_to(c, vid, frac):
    p = plan(c, vid)
    total = sum(haversine_m(*p["route"]["polyline"][i], *p["route"]["polyline"][i + 1]) for i in range(len(p["route"]["polyline"]) - 1))
    st = c.app.state.ctx.nav.state[vid]
    st["simulated"], st["sim_along"] = True, total * frac
    return total


# ---- plan once, commit -----------------------------------------------------------------------------------------------------
def test_normal_driving_keeps_the_committed_route_and_logs_every_evaluation(trip):
    c, ctx, iid, vid, hid = trip
    first = plan(c, vid)["route"]["polyline"]
    for _ in range(4):
        move(c, vid, 0.3)
    p = plan(c, vid)
    assert p["route"]["polyline"] == first and p["route_lock"] == {**p["route_lock"], "locked": True, "version": 1, "reroutes": 0}
    assert p["route_notice"] is None and p["off_route"] is None and p["backups"] == []
    ds = decisions(c, iid)
    assert len(ds) >= 4 and all(d["payload"]["decision"] == "keep" and d["payload"]["policy_version"] == "stable-v1" for d in ds)
    assert all("NO_MATERIAL_CHANGE" in d["payload"]["reason_codes"] and "remaining_eta_s" in d["payload"]["numbers"] for d in ds)
    assert all(d["ledger"]["entry_hash"] for d in ds)                          # every evaluation is a ledger entry
    assert c.portal.call(ctx.ledger.verify_result)["ok"]


# ---- wrong turn: rejoin first -----------------------------------------------------------------------------------------------
def test_wrong_turn_is_confirmed_after_three_good_fixes_and_recovered_by_rejoining(trip):
    c, ctx, iid, vid, hid = trip
    move(c, vid, 0.3)
    original = [tuple(x) for x in plan(c, vid)["route"]["polyline"]]
    r = c.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=drv(vid), params={"offset_m": 220})
    assert r.status_code == 200 and r.json()["simulated"] is True
    move(c, vid, 0.2)
    p = plan(c, vid)
    assert p["off_route"]["state"] == "suspect" and p["route_notice"] is None                   # one bad fix is not a wrong turn
    move(c, vid, 0.2)
    assert plan(c, vid)["route_notice"] is None
    move(c, vid, 0.2)                                                                              # third consecutive fix: confirmed
    p = plan(c, vid)
    note = p["route_notice"]
    assert note and note["reason"] == "wrong_turn" and note["kind"] in ("rejoin", "reroute")
    assert "OFF_ROUTE_CONFIRMED" in note["reason_codes"] and note["message"].startswith("Off route")
    if note["kind"] == "rejoin":
        assert "Rejoin in" in note["message"]
        tail = [tuple(x) for x in p["route"]["polyline"]][-5:]
        assert all(rp.cross_track_m(original, t) < 1.0 for t in tail)                       # the end of the ORIGINAL route is kept
    assert p["off_route"] is None and p["route_lock"]["reroutes"] == 1 and p["route_lock"]["version"] == 2
    ch = [e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json() if e["event_type"] == "route.changed"][0]
    assert ch["payload"]["same_destination"] is True and ch["payload"]["reason"] == "wrong_turn"
    inc = c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]
    assert inc["assigned_vehicle_id"] == vid and inc["destination_id"] == hid
    d = [x for x in decisions(c, iid) if x["payload"]["decision"] != "keep"][0]
    assert d["payload"]["numbers"]["off_count"] >= 3 and d["payload"]["numbers"]["cross_track_m"] > 45
    assert c.post(f"/api/driver/{vid}/route-notice/{note['id']}/ack", headers=drv(vid)).json()["acknowledged"]
    for _ in range(3):
        move(c, vid, 0.3)                                                                            # driving on after the rejoin: stays locked
    assert plan(c, vid)["route_lock"]["reroutes"] == 1


def test_two_off_route_fixes_then_back_on_route_changes_nothing(trip):
    c, ctx, iid, vid, hid = trip
    move(c, vid, 0.3)
    c.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=drv(vid), params={"offset_m": 220})
    move(c, vid, 0.2)
    move(c, vid, 0.2)
    ctx.nav.state[vid]["offset"] = None                                                                  # the driver was back on the road
    move(c, vid, 0.2)
    p = plan(c, vid)
    assert p["route_notice"] is None and p["route_lock"]["reroutes"] == 0 and p["off_route"] is None


def test_poor_gps_accuracy_means_uncertain_and_no_route_change(trip):
    c, ctx, iid, vid, hid = trip
    p = plan(c, vid)
    pl = [tuple(x) for x in p["route"]["polyline"]]
    far = (pl[2][0] + 0.004, pl[2][1] + 0.004)
    for _ in range(6):
        assert c.portal.call(lambda: ctx.stability.on_fix(vid, far[0], far[1], accuracy_m=120.0)) == "uncertain"
    p = plan(c, vid)
    assert p["off_route"]["state"] == "uncertain" and "poor" in p["off_route"]["message"]
    assert p["route_notice"] is None and p["route_lock"]["reroutes"] == 0
    d = c.portal.call(lambda: ctx.stability.evaluate(vid, force=True))
    assert d.kind == "keep" and "GPS_ACCURACY_POOR_NO_ACTION" in d.reasons


def test_simulating_a_wrong_turn_is_validated_and_scoped(trip):
    c, ctx, iid, vid, hid = trip
    assert c.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=drv(vid), params={"offset_m": 5}).status_code == 422
    assert c.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=token_for("driver-AMB-20"), params={"offset_m": 200}).status_code == 403
    assert c.post("/api/driver/AMB-19/simulate-wrong-turn", headers=DISP, params={"offset_m": 200}).status_code in (409, 422)


# ---- blocked road ----------------------------------------------------------------------------------------------------------------
def test_blocked_road_ahead_reroutes_at_once_and_still_acts_in_the_final_approach(trip):
    c, ctx, iid, vid, hid = trip
    total = drive_to(c, vid, 0.75)                                     # about 260 m from the end: inside the 300 m final approach
    c.portal.call(lambda: ctx.nav.advance(vid, 0.0))
    p0 = plan(c, vid)
    assert p0["remaining_m"] <= 300
    pl = [tuple(x) for x in p0["route"]["polyline"]]
    spot = point_at(pl, total * 0.86)
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "closure", "lat": spot[0], "lon": spot[1], "radius_m": 30})
    p = plan(c, vid)
    assert p["route_notice"]["reason"] == "blocked" and "ROAD_BLOCKED" in p["route_notice"]["reason_codes"]
    assert not path_hits_circle([tuple(x) for x in p["route"]["polyline"]], spot, 30)
    blocked = [d for d in decisions(c, iid) if "ROAD_BLOCKED" in d["payload"]["reason_codes"]]
    assert blocked and blocked[0]["payload"]["decision"] in ("rejoin", "reroute")


# ---- congestion: only when worth it ----------------------------------------------------------------------------------------------------
def test_mild_congestion_is_kept_with_reasons_and_never_changes_the_route(trip):
    c, ctx, iid, vid, hid = trip
    pl = [tuple(x) for x in plan(c, vid)["route"]["polyline"]]
    mid = pl[len(pl) // 2]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "lat": mid[0], "lon": mid[1], "radius_m": 200, "multiplier": 1.15})
    for _ in range(4):
        move(c, vid, 0.2)
    p = plan(c, vid)
    assert p["route_notice"] is None and p["route_lock"]["reroutes"] == 0
    kept = [d for d in decisions(c, iid)]
    assert kept and all(d["payload"]["decision"] == "keep" for d in kept)
    assert any("NO_MATERIAL_CHANGE" in d["payload"]["reason_codes"] for d in kept)


def test_heavy_congestion_needs_two_evaluations_in_a_row_before_any_action(trip):
    c, ctx, iid, vid, hid = trip
    pl = [tuple(x) for x in plan(c, vid)["route"]["polyline"]]
    mid = pl[len(pl) // 2]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "lat": mid[0], "lon": mid[1], "radius_m": 2500, "multiplier": 6.0})
    # adding the disruption already triggered the first evaluation: worse, but not yet sustained, so nothing happened
    first = decisions(c, iid)[-1]["payload"]
    assert first["decision"] == "keep" and "ETA_WORSE_NOT_SUSTAINED" in first["reason_codes"] and first["numbers"]["congestion_count"] == 1
    assert plan(c, vid)["route_notice"] is None
    second = c.portal.call(lambda: ctx.stability.evaluate(vid, force=True))
    assert second.numbers["congestion_count"] == 2 and "ETA_WORSE_SUSTAINED" in second.reasons
    assert second.kind in ("reroute", "keep")                                       # worth-it checks decide; reasons are logged either way
    assert any(r in second.reasons for r in ("WORTH_IT_SAVES_ENOUGH", "SAVING_TOO_SMALL", "NO_CLEAR_ALTERNATIVE", "COOLDOWN_ACTIVE", "FINAL_APPROACH_LOCKED"))
    assert {"alt_eta_s", "saving_s"} <= set(second.numbers) or second.reasons[-1] == "NO_CLEAR_ALTERNATIVE"


def test_cooldown_stops_back_to_back_reroutes(trip):
    """Congestion keeps getting worse and a much faster alternative exists, yet a second reroute inside 60 s is refused."""
    c, ctx, iid, vid, hid = trip
    ctx.stability.policy["congestion"].update(sustained_evaluations=1, eta_increase_pct=1, eta_increase_s=1)

    async def fast_alternative(origin, dest, k=2, disruptions=None):
        r = await ctx.routing.route(origin, dest, disruptions)
        return [r.model_copy(update={"duration_s": 5.0, "duration_low_s": 4.0, "duration_high_s": 6.0, "notes": ["alternative route"]})]

    ctx.routing.alternatives = fast_alternative
    pl = [tuple(x) for x in plan(c, vid)["route"]["polyline"]]
    mid = pl[len(pl) // 2]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "lat": mid[0], "lon": mid[1], "radius_m": 2500, "multiplier": 3.0})
    first = [d for d in decisions(c, iid) if d["payload"]["decision"] == "reroute"]
    assert first and "WORTH_IT_SAVES_ENOUGH" in first[0]["payload"]["reason_codes"]
    assert plan(c, vid)["route_lock"]["reroutes"] == 1
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "lat": mid[0], "lon": mid[1], "radius_m": 2500, "multiplier": 6.0})
    d2 = c.portal.call(lambda: ctx.stability.evaluate(vid, force=True))
    assert d2.kind == "keep" and "COOLDOWN_ACTIVE" in d2.reasons and d2.numbers["since_last_reroute_s"] < 60
    assert plan(c, vid)["route_lock"]["reroutes"] == 1


# ---- logging, approvals, determinism ----------------------------------------------------------------------------------------------------------------
def test_evaluations_are_throttled_by_the_configured_interval(trip):
    c, ctx, iid, vid, hid = trip
    ctx.stability.policy["evaluation"]["interval_s"] = 3600
    n0 = len(decisions(c, iid))
    for _ in range(5):
        move(c, vid, 0.2)
    assert len(decisions(c, iid)) - n0 <= 1


def test_the_policy_can_be_switched_off_for_the_human_approved_mode(trip):
    c, ctx, iid, vid, hid = trip
    ctx.stability.policy["enabled"] = False
    pl = [tuple(x) for x in plan(c, vid)["route"]["polyline"]]
    mid = pl[len(pl) * 2 // 3]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 250})
    assert plan(c, vid)["route_notice"] is None and plan(c, vid)["route_lock"]["reroutes"] == 0


def test_same_inputs_give_the_same_decisions(client):
    def one_run():
        from tests.integration.test_driver_hospital_flow import make_assigned as mk

        client.post("/api/admin/reset-demo", headers=token_for(role="admin"))
        ctx = client.app.state.ctx
        ctx.stability.policy["evaluation"]["interval_s"] = 0
        iid, vid, hid, _ = mk(client)
        a = client.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
        client.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
        set_status(client, vid, "en_route", 1)
        client.get(f"/api/driver/{vid}/plan", headers=drv(vid))
        move(client, vid, 0.3)
        client.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=drv(vid), params={"offset_m": 200})
        for _ in range(4):
            move(client, vid, 0.2)
        return [(d["payload"]["decision"], d["payload"]["reason_codes"]) for d in decisions(client, iid)]

    assert one_run() == one_run()
