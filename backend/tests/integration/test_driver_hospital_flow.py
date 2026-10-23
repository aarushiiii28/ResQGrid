from tests.conftest import token_for

CENTER = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 10}
DISP = token_for(role="dispatcher")


def now_iso(c, plus=0):
    from datetime import timedelta

    return (c.app.state.ctx.now() + timedelta(seconds=plus)).isoformat()


def make_assigned(c, category="cardiac", **kw):
    iid = c.post("/api/incidents", headers=DISP, json={
        "reported_category": category, "operator_priority": 1, "location": CENTER, "text": "collapsed", **kw}).json()["id"]
    c.app.state.ctx.autopilot.enabled = False
    d = c.post("/api/dispatch/recommend", headers=DISP, json={"incident_id": iid, "autopilot": False}).json()
    r = c.post(f"/api/dispatch/{d['id']}/approve", headers=DISP, json={"action": "approve", "reason": "looks right"})
    assert r.status_code == 200, r.text
    vid, hid = d["recommendation"]["vehicle_id"], d["recommendation"]["hospital_id"]
    return iid, vid, hid, r.json()


def drv(vid):
    return token_for(f"driver-{vid}")


def hosp(hid):
    return token_for(f"hosp-{hid}")


def set_status(c, vid, status, n):
    r = c.patch(f"/api/vehicles/{vid}/state", headers=drv(vid), json={
        "update_id": f"t-{vid}-{status}-{n}", "occurred_at": now_iso(c), "status": status})
    assert r.status_code == 200 and r.json()["applied"], r.text


def move(c, vid, run, seconds=100000):
    return run(c.app.state.ctx.nav.advance, vid, seconds)


def test_full_flow_sos_to_handover_with_human_steps(client, run):
    c = client
    iid, vid, hid, dec = make_assigned(c)
    asg = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    assert asg["state"] == "sent"
    # driver: acknowledge, accept, en route
    assert c.post(f"/api/assignments/{asg['id']}/acknowledge", headers=drv(vid)).json()["state"] == "acknowledged"
    assert c.post(f"/api/assignments/{asg['id']}/accept", headers=drv(vid)).json()["state"] == "accepted"
    set_status(c, vid, "en_route", 1)
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["phase"] == "scene" and plan["route"]["steps"] and plan["remaining_m"] > 0
    assert plan["green_corridor"]["simulation"] is True and "simulation" in plan["green_corridor"]["label"].lower()
    assert plan["next_step"] and plan["next_step"]["instruction"]
    # simulated GPS carries the unit to the scene
    assert move(c, vid, run)["done"]
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["remaining_m"] < 5 and plan["simulated_movement"] is True and plan["last_100m"] is True
    set_status(c, vid, "on_scene", 2)
    set_status(c, vid, "transporting", 3)
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["phase"] == "hospital" and plan["target"]["kind"] == "hospital"
    assert move(c, vid, run)["done"]
    # pre-alert exists since approval; hospital sees it and works the case
    h = hosp(hid)
    pas = c.get("/api/prealerts", headers=h).json()
    assert [p["incident_id"] for p in pas] == [iid] and pas[0]["status"] == "incoming"
    pid = pas[0]["id"]
    assert c.post(f"/api/prealerts/{pid}/acknowledge", headers=h).json()["status"] == "acknowledged"
    arr = c.post(f"/api/driver/{vid}/arrived", headers=drv(vid))
    assert arr.status_code == 200
    pa = c.get(f"/api/prealerts/{pid}", headers=h).json()
    assert pa["status"] == "arrived" and pa["arrival_source"] == "driver" and pa["acknowledged_at"]
    hs = c.post(f"/api/prealerts/{pid}/handover_started", headers=h).json()
    assert hs["status"] == "handover_started"
    done = c.post(f"/api/prealerts/{pid}/handover_completed", headers=h).json()
    assert done["status"] == "handover_completed" and done["handover_duration_s"] >= 0
    # vehicle is free again and the case is closed, with a complete audit trail
    assert [v for v in c.get("/api/vehicles", headers=DISP).json() if v["id"] == vid][0]["status"] == "available"
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["status"] == "closed"
    types = [e["event_type"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()]
    order = ["incident.created", "dispatch.recommended", "assignment.sent", "dispatch.approved", "prealert.sent",
             "assignment.accepted", "prealert.acknowledged", "vehicle.arrived_at_hospital", "prealert.arrived",
             "prealert.handover_started", "prealert.handover_completed", "incident.closed"]
    idx = [types.index(t) for t in order]
    assert idx == sorted(idx)


def test_prealert_separates_reported_from_confirmed_and_lists_unknowns(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    c.post(f"/api/incidents/{iid}/facts", headers=DISP, json={"key": "conscious", "value": False, "source": "reported"})
    c.post(f"/api/incidents/{iid}/facts", headers=DISP, json={"key": "bleeding", "value": True, "source": "human_confirmed"})
    c.app.state.ctx.portal = None
    pa = c.get("/api/prealerts", headers=hosp(hid)).json()[0]
    conf = {o["key"]: o for o in pa["confirmed_observations"]}
    rep = {o["key"] for o in pa["reported_observations"]}
    assert conf["reported_category"]["source"] == "human_confirmed"
    assert pa["reported_reason"].startswith("Cardiac") and "human confirmed" in pa["reported_reason"]
    assert "unknowns" in pa and "Number of patients not known" in pa["unknowns"]
    assert pa["eta_low_s"] and pa["eta_low_s"] < pa["eta_high_s"] and pa["vehicle_id"] == vid
    assert not (set(conf) & rep)


def test_prealert_refreshes_when_facts_change_and_checklist_is_updatable(client, run):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    c.post(f"/api/incidents/{iid}/facts", headers=DISP, json={"key": "patient_count", "value": 2, "source": "human_confirmed"})
    run(c.app.state.ctx.cases.refresh, iid)
    pa = c.get("/api/prealerts", headers=hosp(hid)).json()[0]
    assert "Number of patients not known" not in pa["unknowns"]
    items = {i["key"]: i for i in pa["checklist"]}
    assert items["patient_count"]["status"] == "provided" and items["age"]["status"] == "open"
    r = c.post(f"/api/prealerts/{pa['id']}/checklist", headers=hosp(hid), json={"key": "age", "status": "provided", "note": "about 60"})
    assert {i["key"]: i for i in r.json()["checklist"]}["age"]["note"] == "about 60"
    assert c.post(f"/api/prealerts/{pa['id']}/checklist", headers=hosp(hid), json={"key": "nope", "status": "open"}).status_code == 409


def test_hospital_sees_only_assigned_cases_and_cannot_touch_others(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    other = "HOSP-03" if hid != "HOSP-03" else "HOSP-04"
    assert c.get("/api/prealerts", headers=hosp(other)).json() == []
    pid = c.get("/api/prealerts", headers=hosp(hid)).json()[0]["id"]
    assert c.get(f"/api/prealerts/{pid}", headers=hosp(other)).status_code == 403
    for action in ("acknowledge", "arrival", "handover_started", "handover_completed"):
        assert c.post(f"/api/prealerts/{pid}/{action}", headers=hosp(other)).status_code == 403
    assert c.post(f"/api/prealerts/{pid}/acknowledge", headers=token_for("caller-1")).status_code == 403
    assert c.post(f"/api/prealerts/{pid}/acknowledge", headers=DISP).status_code == 403  # dispatchers read, hospitals act
    assert c.get("/api/incidents", headers=hosp(other)).json() == []


def test_handover_order_is_enforced(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    h = hosp(hid)
    pid = c.get("/api/prealerts", headers=h).json()[0]["id"]
    assert c.post(f"/api/prealerts/{pid}/handover_started", headers=h).status_code == 409
    assert c.post(f"/api/prealerts/{pid}/handover_completed", headers=h).status_code == 409
    assert c.post(f"/api/prealerts/{pid}/arrival", headers=h).json()["arrival_source"] == "hospital"
    assert c.post(f"/api/prealerts/{pid}/handover_completed", headers=h).status_code == 409
    assert c.post(f"/api/prealerts/{pid}/handover_started", headers=h).status_code == 200
    # repeating a step is idempotent, not an error or a duplicate
    again = c.post(f"/api/prealerts/{pid}/handover_started", headers=h).json()
    assert again["status"] == "handover_started"


def test_changing_destination_supersedes_old_prealert_openly(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    d2 = c.post("/api/dispatch/recommend", headers=DISP, json={"incident_id": iid, "autopilot": False,
                                                                 "weights": {"w_switch": 0}}).json()
    alt = next((x for x in d2["candidates"] if x["hospital_id"] != hid and x["vehicle_id"] == vid), None)
    if alt is None:
        return  # no second capable hospital near this unit; nothing to switch to
    r = c.post(f"/api/dispatch/{d2['id']}/approve", headers=DISP,
               json={"action": "override", "reason": "closer cath lab", "hospital_id": alt["hospital_id"]})
    assert r.status_code == 200, r.text
    old = c.get("/api/prealerts", headers=hosp(hid)).json()
    assert old[0]["status"] == "superseded" and "destination changed" in old[0]["superseded_reason"]
    assert c.post(f"/api/prealerts/{old[0]['id']}/acknowledge", headers=hosp(hid)).status_code == 409
    new = c.get("/api/prealerts", headers=hosp(alt["hospital_id"])).json()
    assert new and new[0]["status"] == "incoming"
    types = [e["event_type"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()]
    assert "prealert.superseded" in types


def test_arrived_requires_transporting_and_own_vehicle(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    assert c.post(f"/api/driver/{vid}/arrived", headers=drv(vid)).status_code == 409
    other = "AMB-02" if vid != "AMB-02" else "AMB-03"
    assert c.post(f"/api/driver/{vid}/arrived", headers=drv(other)).status_code == 403
    assert c.get(f"/api/driver/{vid}/plan", headers=drv(other)).status_code == 403
    assert c.get(f"/api/driver/{vid}/plan", headers=hosp(hid)).status_code == 403


def test_driver_me_returns_assignment_brief_and_plan(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    me = c.get("/api/driver/me", headers=drv(vid)).json()
    assert me["assignment"]["state"] == "sent" and me["incident"]["id"] == iid
    assert me["plan"]["route"]["source"] == "mock" and me["incident"]["hospital"]["id"] == hid
    assert me["incident"]["arrived"] is False and me["incident"]["handover_status"] == "incoming"
    idle = c.get("/api/driver/me", headers=drv("AMB-19" if vid != "AMB-19" else "AMB-18")).json()
    assert idle["assignment"] is None and idle["incident"] is None and idle["plan"] is None


def test_closure_reroutes_at_once_around_it_tells_the_dispatcher_and_the_driver_only_acknowledges(client, run):
    from app.routing.geometry import path_hits_circle

    c = client
    iid, vid, hid, _ = make_assigned(c)
    a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    c.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    set_status(c, vid, "en_route", 1)
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["reroute"] is None and plan["route_notice"] is None
    assert plan["route_lock"]["locked"] and plan["route_lock"]["version"] == 1 and plan["route_lock"]["reroutes"] == 0
    pl = [tuple(p) for p in plan["route"]["polyline"]]
    mid = pl[len(pl) * 2 // 3]
    cl = c.post("/api/routing/disruptions", headers=DISP, json={"kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 250})
    assert cl.status_code == 201
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    note = plan["route_notice"]
    assert plan["reroute"] is None                                           # not a proposal waiting for a tap
    assert note and note["reason"] == "blocked" and note["kind"] in ("rejoin", "reroute") and note["acknowledged"] is False
    assert not path_hits_circle([tuple(p) for p in plan["route"]["polyline"]], mid, 250)    # already avoids the closure
    assert plan["route_lock"]["version"] == 2 and plan["route_lock"]["reroutes"] == 1
    assert "ROAD_BLOCKED" in note["reason_codes"]
    alerts = c.get("/api/alerts", headers=DISP).json()
    assert any(x["kind"] == "route_changed" and vid in x["text"] and "Same destination" in x["text"] for x in alerts)
    assert c.post(f"/api/driver/{vid}/route-notice/{note['id']}/ack", headers=DISP).status_code == 403     # only the driver
    ok = c.post(f"/api/driver/{vid}/route-notice/{note['id']}/ack", headers=drv(vid))
    assert ok.status_code == 200 and ok.json()["acknowledged"] is True
    assert c.post(f"/api/driver/{vid}/route-notice/RN-9999/ack", headers=drv(vid)).status_code == 409
    assert not any(x["kind"] == "route_changed" for x in c.get("/api/alerts", headers=DISP).json())
    ev = {e["event_type"]: e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()}
    assert "route.committed" in ev and "route.decision" in ev and "route.changed" in ev and "nav.route_notice_acknowledged" in ev
    assert ev["route.changed"]["payload"]["same_destination"] is True
    inc = c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]
    assert inc["assigned_vehicle_id"] == vid and inc["destination_id"] == hid     # vehicle and destination untouched


def test_route_is_committed_with_up_to_two_prechecked_backups_shown_only_when_relevant(client):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    c.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    set_status(c, vid, "en_route", 1)
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["backups"] == []                                              # dim backups only appear when relevant
    st = c.app.state.ctx.nav.state[vid]["stab"]
    assert 1 <= len(st["backups"]) <= 2 and all(b.feasible is True for b in st["backups"])
    assert st["backups"][0].polyline != [tuple(p) for p in plan["route"]["polyline"]]
    ev = [e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json() if e["event_type"] == "route.committed"][0]
    assert ev["payload"]["backups"] and ev["payload"]["policy_version"] == "stable-v1"


def test_congestion_shows_eta_drift_in_plan(client, run):
    c = client
    iid, vid, hid, _ = make_assigned(c)
    a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    c.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    set_status(c, vid, "en_route", 1)
    before = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "multiplier": 3.0, "note": "event crowd"})
    after = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert after["remaining_s"] > before["remaining_s"] * 2.5  # honest ETA, not a stale optimistic one
    assert after["remaining_low_s"] < after["remaining_s"] < after["remaining_high_s"]


def test_simulated_movement_endpoint_moves_unit_and_labels_it(sim_client):
    c = sim_client
    c.app.state.ctx.autopilot.enabled = False
    iid, vid, hid, _ = make_assigned(c)
    a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
    c.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    set_status(c, vid, "en_route", 1)
    start = [v for v in c.get("/api/vehicles", headers=DISP).json() if v["id"] == vid][0]["location"]
    r = c.post(f"/api/driver/{vid}/simulate-movement", headers=drv(vid))
    assert r.json()["label"] == "Simulated GPS movement"
    c.portal.call(c.app.state.ctx.drain)
    end = [v for v in c.get("/api/vehicles", headers=DISP).json() if v["id"] == vid][0]
    assert end["location"]["source"] == "simulated" and (end["location"]["lat"], end["location"]["lon"]) != (start["lat"], start["lon"])
    assert c.post(f"/api/driver/{vid}/simulate-movement", headers=token_for("driver-AMB-17")).status_code in (403, 409)


def test_alerts_strip_shows_timeout_with_pulse_and_stale_gps(client, ctx):
    from datetime import timedelta

    c = client
    iid, vid, hid, _ = make_assigned(c)
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.vehicle_stale_s + 30)
    c.post("/api/assignments/check-timeouts", headers=DISP)
    alerts = c.get("/api/alerts", headers=DISP).json()
    kinds = {a["kind"] for a in alerts}
    assert "no_acknowledgment" in kinds and any(a["pulse"] for a in alerts if a["kind"] == "no_acknowledgment")
    assert any(a["kind"] == "stale_gps" and a["entity"] == vid for a in alerts)
    assert all(a["text"] for a in alerts) and alerts[0]["severity"] == "red"
    assert c.get("/api/alerts", headers=token_for("caller-1")).status_code == 403
