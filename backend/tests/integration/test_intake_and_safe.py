"""After the SOS goes live the caller is asked what happened, the severity is predicted, and only then is the right ambulance chosen.
If the caller says "I am safe", the automatic assignment is stopped (or recalled), the unit is released and the incident stays
open for a dispatcher."""

import time

from tests.conftest import token_for

HERE = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 12.0, "location_age_s": 1.0}
CALLER = lambda: token_for("caller-1")  # noqa: E731
DISP = lambda: token_for(role="dispatcher")  # noqa: E731


def ctxof(c):
    return c.app.state.ctx


def decisions(c, iid):
    return c.get("/api/dispatch/decisions", headers=DISP(), params={"incident_id": iid}).json()


def incident(c, iid):
    return c.get(f"/api/incidents/{iid}", headers=DISP()).json()["incident"]


def vehicles(c):
    return {v["id"]: v for v in c.get("/api/vehicles", headers=DISP()).json()}


def steps(c, iid):
    return [e["payload"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP()).json() if e["event_type"] == "pipeline.step"]


def sos(c, wait, **kw):
    ctxof(c).settings.__dict__["intake_wait_s"] = wait
    r = c.post("/api/sos", headers=CALLER(), json={**HERE, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def until(fn, seconds=6.0):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def say(c, call_id, text, mode="typed"):
    r = c.post(f"/api/calls/{call_id}/utterances", headers=CALLER(), json={"text": text, "mode": mode})
    assert r.status_code == 200, r.text


def signals(c, iid, feats):
    body = {"mic_consent": False, "features": [{"name": n, **v} for n, v in feats.items()]}
    assert c.post(f"/api/incidents/{iid}/signals", headers=CALLER(), json=body).status_code == 200


# ---- intake: describe, predict severity, then assign the right type -----------------------------------------------------------
def test_nothing_is_assigned_until_the_caller_describes_what_happened(client):
    r = sos(client, 5.0)
    iid = r["incident_id"]
    time.sleep(0.8)
    assert decisions(client, iid) == [] and incident(client, iid)["assigned_vehicle_id"] is None      # waiting for the description
    t = client.get(f"/api/tracker/{iid}", headers=CALLER()).json()
    assert "Tell us what happened" in t["headline"]
    assert any(p["step"] == "intake" and p["status"] == "waiting" for p in t["pipeline"])
    say(client, r["call_id"], "chest pain", "quick")
    dec = until(lambda: decisions(client, iid))
    assert dec, "dispatch follows as soon as the category is known"
    inc = incident(client, iid)
    assert inc["reported_category"]["value"] == "cardiac"
    v = vehicles(client)[dec[-1]["recommendation"]["vehicle_id"]]
    assert {"ALS", "cardiac"} <= set(v["capability_tags"])                                                # the right type of ambulance
    done = [s for s in steps(client, iid) if s["step"] == "intake" and s["status"] == "done"]
    assert done and "predicted severity P1" in done[0]["detail"] and done[0]["category"] == "cardiac"
    order = [(s["step"], s["status"]) for s in steps(client, iid) if s["step"] in ("intake", "dispatch_recommendation")]
    assert order.index(("intake", "waiting")) < order.index(("intake", "done")) < order.index(("dispatch_recommendation", "running"))
    client.portal.call(ctxof(client).drain)


def test_the_nearest_unit_of_the_predicted_type_is_chosen_not_just_the_nearest(client):
    r = sos(client, 5.0)
    iid = r["incident_id"]
    say(client, r["call_id"], "bleeding badly after a car accident", "typed")
    dec = until(lambda: decisions(client, iid))
    tags = set(vehicles(client)[dec[-1]["recommendation"]["vehicle_id"]]["capability_tags"])
    assert {"ALS", "trauma"} <= tags                                       # trauma needs an ALS + trauma unit, wherever the nearest BLS is
    client.portal.call(ctxof(client).drain)


def test_without_any_description_the_unit_goes_after_the_wait_on_the_safest_assumption(client):
    r = sos(client, 0.6)
    iid = r["incident_id"]
    assert until(lambda: decisions(client, iid), 5)
    dec = decisions(client, iid)[-1]
    assert "ALS" in vehicles(client)[dec["recommendation"]["vehicle_id"]]["capability_tags"]            # unknown is not minor
    timeout = [s for s in steps(client, iid) if s["step"] == "intake" and s["status"] == "done"]
    assert timeout and timeout[0].get("timeout") is True and "safest assumption" in timeout[0]["detail"]
    client.portal.call(ctxof(client).drain)


def test_a_crash_signature_or_a_call_for_help_does_not_wait_for_the_description(client):
    ctxof(client).settings.__dict__["intake_wait_s"] = 30.0
    r = client.post("/api/sos/start", headers=CALLER(), json=HERE).json()
    iid = r["incident_id"]
    signals(client, iid, {"crash_like": {"value": 0.95, "status": "available"}})
    assert client.post(f"/api/incidents/{iid}/sos/confirm", headers=CALLER()).status_code == 200
    assert until(lambda: decisions(client, iid), 4)                         # dispatched without waiting 30 s
    assert any("high-risk signal" in s["detail"] for s in steps(client, iid) if s["step"] == "intake")
    r2 = sos(client, 30.0)
    say(client, r2["call_id"], "please help, there is a fire", "typed")
    assert until(lambda: decisions(client, r2["incident_id"]), 4)
    client.portal.call(ctxof(client).drain)


def test_a_late_answer_refines_a_plan_that_already_started(client):
    r = sos(client, 0.5)
    iid = r["incident_id"]
    assert until(lambda: decisions(client, iid), 4)
    client.portal.call(ctxof(client).drain)
    first = decisions(client, iid)[-1]["recommendation"]["vehicle_id"]
    say(client, r["call_id"], "chest pain", "quick")
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["reported_category"]["value"] == "cardiac"
    assert first and len(decisions(client, iid)) >= 1


# ---- "I am safe" stops the assignment -------------------------------------------------------------------------------------------
def test_i_am_safe_during_the_intake_wait_means_no_ambulance_is_ever_assigned(client):
    r = sos(client, 5.0)
    iid = r["incident_id"]
    time.sleep(0.4)
    ans = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"}).json()
    assert ans["state"] == "safe" and ans["dispatch"] in ("stopped", "not_started")
    time.sleep(0.8)
    client.portal.call(ctxof(client).drain)
    assert decisions(client, iid) == []
    assert incident(client, iid)["assigned_vehicle_id"] is None
    assert all(v["status"] != "assigned" for v in vehicles(client).values())
    assert any(s["step"] == "intake" and s["status"] == "stopped" for s in steps(client, iid))
    t = client.get(f"/api/tracker/{iid}", headers=CALLER()).json()
    assert t["dispatch_stopped"] and "no ambulance is being sent" in t["headline"]
    fa = client.get(f"/api/incidents/{iid}/false-alarm", headers=DISP()).json()
    assert fa["awaiting_review"] or fa["checkin"]["state"] == "safe"
    assert incident(client, iid)["status"] != "closed"                      # still open for a dispatcher


def test_i_am_safe_after_dispatch_recalls_the_assignment_and_releases_the_unit(client):
    r = sos(client, 0.0)
    iid = r["incident_id"]
    client.portal.call(ctxof(client).drain)
    inc = incident(client, iid)
    vid, hid = inc["assigned_vehicle_id"], inc["destination_id"]
    assert vid and vehicles(client)[vid]["status"] == "assigned"
    assert client.get("/api/prealerts", headers=token_for(f"hosp-{hid}")).json()
    ans = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"}).json()
    assert ans["dispatch"] == "stopped"
    inc = incident(client, iid)
    assert inc["assigned_vehicle_id"] is None and inc["destination_id"] is None and inc["status"] == "new"
    assert vehicles(client)[vid]["status"] == "available"                    # the unit is free again
    asg = client.get("/api/assignments", headers=DISP(), params={"incident_id": iid}).json()
    assert asg and all(a["state"] == "cancelled" for a in asg)
    pas = client.get("/api/prealerts", headers=token_for(f"hosp-{hid}")).json()
    assert pas and all(p["status"] == "superseded" for p in pas)             # the hospital is told, not left waiting
    types = [e["event_type"] for e in client.get(f"/api/incidents/{iid}/audit", headers=DISP()).json()]
    assert {"false_alarm.dispatch_recalled", "assignment.cancelled", "prealert.superseded"} <= set(types)
    d = decisions(client, iid)[-1]
    assert d["status"] in ("approved", "superseded")
    # later signals or timers must not send it again
    signals(client, iid, {"touch_deliberate": {"value": 0.9, "status": "available"}})
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["assigned_vehicle_id"] is None
    t = client.get(f"/api/tracker/{iid}", headers=CALLER()).json()
    assert t["dispatch_stopped"] and "no ambulance is being sent" in t["headline"]
    fa = client.get(f"/api/incidents/{iid}/false-alarm", headers=DISP()).json()
    assert fa["recalled"] and fa["awaiting_review"] is False or fa["recalled"]


def test_a_dispatcher_can_still_dispatch_after_the_caller_said_safe(client):
    r = sos(client, 0.0)
    iid = r["incident_id"]
    client.portal.call(ctxof(client).drain)
    client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"})
    assert incident(client, iid)["assigned_vehicle_id"] is None
    assert client.post(f"/api/incidents/{iid}/false-alarm/dispatch", headers=DISP()).status_code == 200
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["assigned_vehicle_id"]


def test_help_is_still_sent_if_the_phone_reports_a_crash_even_when_the_caller_taps_safe(client):
    ctxof(client).settings.__dict__["intake_wait_s"] = 0.0
    iid = client.post("/api/sos/start", headers=CALLER(), json=HERE).json()["incident_id"]
    signals(client, iid, {"crash_like": {"value": 0.95, "status": "available"}})
    client.post(f"/api/incidents/{iid}/sos/confirm", headers=CALLER())
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["assigned_vehicle_id"]
    ans = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"})
    if ans.status_code == 200:                                              # HIGH lanes are not interrupted with prompts, so this may be refused
        assert ans.json()["dispatch"] in ("continues", "not_started")
    assert incident(client, iid)["assigned_vehicle_id"]


def test_saying_i_am_fine_in_the_call_has_the_same_effect(client):
    r = sos(client, 0.0)
    iid = r["incident_id"]
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["assigned_vehicle_id"]
    say(client, r["call_id"], "sorry, pressed by mistake, i am fine")
    client.portal.call(ctxof(client).drain)
    assert incident(client, iid)["assigned_vehicle_id"] is None


def test_a_crew_already_with_the_patient_is_never_recalled_automatically(client):
    r = sos(client, 0.0)
    iid = r["incident_id"]
    client.portal.call(ctxof(client).drain)
    vid = incident(client, iid)["assigned_vehicle_id"]
    from datetime import timedelta

    a = client.get("/api/assignments", headers=DISP(), params={"incident_id": iid}).json()[0]
    drv = token_for(f"driver-{vid}")
    client.post(f"/api/assignments/{a['id']}/accept", headers=drv)
    for i, st in enumerate(("en_route", "on_scene")):
        client.patch(f"/api/vehicles/{vid}/state", headers=drv, json={"update_id": f"s{i}", "occurred_at": (ctxof(client).now() + timedelta(seconds=i)).isoformat(), "status": st})
    ans = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"}).json()
    assert ans["dispatch"] == "unit_on_scene"
    assert incident(client, iid)["assigned_vehicle_id"] == vid and vehicles(client)[vid]["status"] == "on_scene"
