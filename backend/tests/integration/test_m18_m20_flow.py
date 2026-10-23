"""Phase 11: the three innovations working together, end to end.

accidental press cancelled (record kept) -> real SOS with a crash signal -> lane HIGH -> dispatch -> driver wrong turn ->
rejoin -> mild congestion kept -> ledger entries signed and anchored -> verify integrity passes -> tamper demo detected."""

from tests.conftest import token_for
from tests.integration.test_ledger import Device
from tests.integration.test_ledger import verify as ledger_verify

HERE = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 9.0, "location_age_s": 1.0}
DISP = token_for(role="dispatcher")
ADMIN = token_for(role="admin")


def drain(c):
    c.portal.call(c.app.state.ctx.drain)


def audit(c, iid):
    return c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()


def plan(c, vid):
    return c.get(f"/api/driver/{vid}/plan", headers=token_for(f"driver-{vid}")).json()


def advance(c, vid, s):
    c.portal.call(lambda: c.app.state.ctx.nav.advance(vid, s))


def test_accidental_press_then_real_sos_then_stable_route_then_verified_ledger(client):
    c = client
    ctx = c.app.state.ctx
    ctx.stability.policy["evaluation"]["interval_s"] = 0
    ctx.false_alarm.base_cfg["tz_offset_h"] = 0
    caller = Device(c, "caller-1")

    # 1. an accidental press: pending incident, cancelled during the countdown, record kept, one check-in queued ----------------------
    r = caller.post("/api/sos/start", {**HERE, "device_id": "dev-phase11"}, "sos.start")
    assert r.status_code == 201 and r.json()["receipt"]["event_type"] == "incident.pending_created"
    accidental = r.json()["incident_id"]
    assert c.get(f"/api/incidents/{accidental}", headers=DISP).json()["incident"]["status"] == "pending"
    assert c.post(f"/api/incidents/{accidental}/sos/cancel", headers=token_for("caller-1")).status_code == 200
    inc = c.get(f"/api/incidents/{accidental}", headers=DISP).json()["incident"]
    assert inc["status"] == "cancelled_by_caller" and inc["assigned_vehicle_id"] is None
    assert c.get(f"/api/incidents/{accidental}/check-in", headers=token_for("caller-1")).json()["prompt"] is True
    drain(c)
    assert c.get("/api/dispatch/decisions", headers=DISP, params={"incident_id": accidental}).json() == []
    assert accidental in {i["id"] for i in c.get("/api/incidents", headers=DISP).json()}              # nothing deleted

    # 2. a real SOS with a crash signal: lane HIGH, dispatched automatically ---------------------------------------------------------
    r = caller.post("/api/sos/start", {**HERE, "device_id": "dev-phase11"}, "sos.start")
    iid = r.json()["incident_id"]
    sig = {"mic_consent": False, "features": [
        {"name": "crash_like", "value": 0.92, "status": "available"},
        {"name": "touch_random", "value": 0.9, "status": "available"},       # looks accidental...
        {"name": "pocket_motion", "value": 0.8, "status": "available"}]}      # ...but a crash signature overrides it
    assert c.post(f"/api/incidents/{iid}/signals", headers=token_for("caller-1"), json=sig).status_code == 200
    confirm = caller.post(f"/api/incidents/{iid}/sos/confirm", {}, "sos.confirm")
    assert confirm.status_code == 200 and confirm.json()["receipt"]["event_type"] == "incident.went_live"
    fa = c.get(f"/api/incidents/{iid}/false-alarm", headers=DISP).json()
    assert fa["lane"] == "HIGH" and "crash_like" in fa["overrides"] and fa["model_version"] == "fa-v1-heuristic"
    drain(c)
    inc = c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]
    assert inc["assigned_vehicle_id"] and inc["destination_id"]
    vid = inc["assigned_vehicle_id"]
    assert fa["checkin"]["state"] == "none"                                                          # HIGH is not interrupted with prompts

    # 3. the crew drives; a wrong turn is recovered by rejoining, not by a fresh plan from scratch ------------------------------------
    a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    c.post(f"/api/assignments/{a['id']}/accept", headers=token_for(f"driver-{vid}"))
    c.patch(f"/api/vehicles/{vid}/state", headers=token_for(f"driver-{vid}"), json={"update_id": "p11-1", "occurred_at": ctx.now().isoformat(), "status": "en_route"})
    base = plan(c, vid)
    assert base["route_lock"]["locked"] and base["route_notice"] is None
    advance(c, vid, 0.3)
    assert c.post(f"/api/driver/{vid}/simulate-wrong-turn", headers=token_for(f"driver-{vid}"), params={"offset_m": 220}).status_code == 200
    for _ in range(3):
        advance(c, vid, 0.2)
    p = plan(c, vid)
    assert p["route_notice"]["reason"] == "wrong_turn" and p["route_notice"]["kind"] in ("rejoin", "reroute")
    assert p["route_lock"]["reroutes"] == 1 and p["off_route"] is None

    # 4. mild congestion: below the thresholds, so the route is kept and the reasons are logged ------------------------------------------
    pl = p["route"]["polyline"]
    mid = pl[len(pl) // 2]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "lat": mid[0], "lon": mid[1], "radius_m": 200, "multiplier": 1.15})
    before = plan(c, vid)["route"]["polyline"]
    for _ in range(3):
        advance(c, vid, 0.2)
    after = plan(c, vid)
    assert after["route_lock"]["reroutes"] == 1
    kept = [e for e in audit(c, iid) if e["event_type"] == "route.decision" and "NO_MATERIAL_CHANGE" in e["payload"]["reason_codes"]]
    assert kept and before[-1] == after["route"]["polyline"][-1]

    # 5. the ledger: signed by the caller's device, anchored, verifiable ------------------------------------------------------------------
    events = audit(c, iid)
    signed = [e for e in events if e["ledger"]["client_signed"]]
    assert {e["event_type"] for e in signed} >= {"incident.pending_created", "incident.went_live"}
    assert all(e["ledger"]["signer_key_id"] == caller.key_id for e in signed)
    assert {e["event_type"] for e in events} >= {"false_alarm.assessed", "dispatch.approved", "route.committed", "route.decision", "route.changed"}
    anchor = c.post("/api/ledger/anchor", headers=ADMIN).json()
    assert anchor["anchor_id"] and anchor["provider"] == "local"
    events = audit(c, iid)
    assert all(e["ledger"]["ledger_status"] in ("ANCHORED", "SIGNED", "PENDING") for e in events)
    assert any(e["ledger"]["ledger_status"] == "ANCHORED" for e in events)
    pending_event = [e for e in events if e["event_type"] == "incident.pending_created"][0]
    proof = c.get(f"/api/ledger/verify/{pending_event['event_id']}", headers=DISP).json()
    assert proof["ok"] and proof["signed_by"] == "client" and proof["merkle_proof_ok"] and proof["status"] == "ANCHORED"
    assert any(e["event_type"] == "false_alarm.assessed" and e["ledger"]["entry_hash"] for e in events)

    # 6. verify integrity passes; the tamper demo is detected on a copy and the real chain stays valid ---------------------------------------
    res = ledger_verify(c)
    assert res["status"] == "VERIFIED" and res["client_signed"] >= 3 and res["anchors"]["checked"] >= 1 and res["failure"] is None
    for mode in ("modify", "delete", "reorder", "insert", "truncate"):
        out = c.post("/api/ledger/tamper-demo", headers=DISP, json={"mode": mode}).json()
        assert out["detected"] and out["real_chain_untouched"] and "DEMONSTRATION" in out["label"], (mode, out)
    drain(c)
    assert ledger_verify(c)["status"] == "VERIFIED"

    # the cancelled press is in the same verified history ---------------------------------------------------------------------------------------
    types = [e["event_type"] for e in audit(c, accidental)]
    assert "incident.cancelled_by_caller" in types and "false_alarm.checkin_prompt" in types
