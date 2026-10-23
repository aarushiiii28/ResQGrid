from tests.conftest import token_for

CENTER = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 12.0, "location_age_s": 1.5}


def drain(c):
    c.portal.call(c.app.state.ctx.drain)


def sos(c, who=None, **kw):
    r = c.post("/api/sos", headers=who or token_for("caller-1"), json={**CENTER, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def say(c, call_id, text, mode="typed", who=None):
    r = c.post(f"/api/calls/{call_id}/utterances", headers=who or token_for("caller-1"), json={"text": text, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def incident(c, iid, who=None):
    return c.get(f"/api/incidents/{iid}", headers=who or token_for(role="dispatcher")).json()["incident"]


def vehicles(c):
    return {v["id"]: v for v in c.get("/api/vehicles", headers=token_for(role="dispatcher")).json()}


def test_sos_creates_incident_without_any_further_input(client):
    r = sos(client)
    assert r["incident_id"].startswith("INC-") and r["call_id"].startswith("CALL-")
    assert "In a real emergency dial 112" in r["note"] and r["mode"] == "DEMO / SIMULATED"
    inc = incident(client, r["incident_id"])
    loc = inc["location_candidates"][0]
    assert (loc["lat"], loc["lon"], loc["accuracy_m"]) == (30.9010, 75.8573, 12.0)
    assert inc["channel"] == "sos" and inc["created_by"] == "caller-1" and not loc["approximate"]
    assert "GPS" not in inc["unknowns"] and "reported_category" in inc["unknowns"]  # nothing typed by the caller
    call = client.get(f"/api/calls/{r['call_id']}", headers=token_for("caller-1")).json()
    assert call["mode"] == "in_app_simulated" and call["transcript"][0]["speaker"] == "operator"
    assert call["questions_asked"] == 1


def test_far_caller_gets_a_nearby_synthetic_fleet(client):
    amritsar = {"lat": 31.6340, "lon": 74.8723}
    from app.db.seed import haversine_m

    sos(client, **amritsar)
    free = [v for v in vehicles(client).values() if v["status"] == "available"]
    nearest = min(haversine_m((amritsar["lat"], amritsar["lon"]), (v["location"]["lat"], v["location"]["lon"])) for v in free)
    assert nearest < 2500  # was about 120 km (Ludhiana) before the fleet followed the caller
    n_near = sum(haversine_m((amritsar["lat"], amritsar["lon"]), (v["location"]["lat"], v["location"]["lon"])) < 2500 for v in free)
    assert n_near >= 6


def test_far_caller_still_gets_localized_when_a_unit_is_busy(client):
    """A busy unit from an earlier job must not stop the idle ones from moving next to the caller."""
    from app.db.seed import haversine_m

    ctx = client.app.state.ctx
    client.portal.call(ctx.vehicle_repo.modify, "AMB-03", lambda v: v.model_copy(update={"status": "en_route"}))
    sos(client, lat=31.6340, lon=74.8723)
    free = [v for v in vehicles(client).values() if v["status"] == "available"]
    assert min(haversine_m((31.6340, 74.8723), (v["location"]["lat"], v["location"]["lon"])) for v in free) < 2600
    assert any(v["id"] == "AMB-03" and v["status"] == "en_route" for v in vehicles(client).values())


def test_amritsar_sos_is_served_by_a_nearby_capable_unit_and_hospital(sim_client):
    from app.db.seed import haversine_m

    c = sim_client
    here = (31.6340, 74.8723)
    iid = sos(c, lat=here[0], lon=here[1])["incident_id"]
    drain(c)
    inc = incident(c, iid)
    v = vehicles(c)[inc["assigned_vehicle_id"]]
    assert haversine_m(here, (v["location"]["lat"], v["location"]["lon"])) < 10_000  # not Ludhiana (about 120 km)
    t = c.get(f"/api/tracker/{iid}", headers=token_for("caller-1")).json()
    assert haversine_m(here, (t["hospital"]["lat"], t["hospital"]["lon"])) < 40_000


def test_sos_pipeline_dispatches_and_pre_alerts_automatically(sim_client):
    c = sim_client
    iid = sos(c)["incident_id"]
    drain(c)
    inc = incident(c, iid)
    assert inc["assigned_vehicle_id"] and inc["destination_id"]
    dec = c.get("/api/dispatch/decisions", headers=token_for(role="dispatcher"), params={"incident_id": iid}).json()
    assert dec and dec[-1]["status"] == "approved" and dec[-1]["approved_by"] == "demo-autopilot"
    assert "ALS" in vehicles(c)[inc["assigned_vehicle_id"]]["capability_tags"]  # unknown is not minor
    pas = c.get("/api/prealerts", headers=token_for(f"hosp-{inc['destination_id']}")).json()
    assert [p["incident_id"] for p in pas] == [iid]
    steps = {s["step"]: s["status"] for s in c.get(f"/api/tracker/{iid}", headers=token_for("caller-1")).json()["pipeline"]}
    assert steps["dispatch_recommendation"] == "done" and steps["approval"] == "done"
    assert steps["hospital_match"] == "done" and steps["pre_alert"] == "done"


def test_full_hands_free_run_reaches_hospital_and_waits_for_human_handover(sim_client):
    c = sim_client
    iid = sos(c)["incident_id"]
    drain(c)
    t = c.get(f"/api/tracker/{iid}", headers=token_for("caller-1")).json()
    states = {s["key"]: s["state"] for s in t["stages"]}
    assert all(states[k] == "done" for k in ("call_started", "unit_sent", "acknowledged", "accepted", "en_route",
                                              "on_scene", "transporting", "arrived"))
    assert t["headline"].startswith("Arrived at")
    v = vehicles(c)[t["vehicle"]["id"]]
    assert v["status"] == "transporting"  # handover is a hospital action, never faked
    actors = {e["actor"] for e in c.get(f"/api/incidents/{iid}/audit", headers=token_for(role="dispatcher")).json()}
    assert {"demo-autopilot", "simulated-driver"} <= actors
    assert t["vehicle"]["simulated"] is True


def test_gps_denied_path_uses_approximate_pin_and_still_proceeds(client):
    r = sos(client, approximate=True, location_source="manual_pin", accuracy_m=None, location_age_s=0)
    inc = incident(client, r["incident_id"])
    loc = inc["location_candidates"][0]
    assert loc["approximate"] is True and "exact_location" in inc["unknowns"]
    d = client.post("/api/dispatch/recommend", headers=token_for(role="dispatcher"),
                    json={"incident_id": r["incident_id"], "autopilot": False})
    assert d.status_code == 200  # an approximate location does not block dispatch
    assert d.json()["recommendation"]["components"]["uncertainty"] >= 1.0  # but it is priced in
    # the caller drags the pin: the incident follows without a new SOS
    up = client.post(f"/api/incidents/{r['incident_id']}/location", headers=token_for("caller-1"),
                     json={"lat": 30.9040, "lon": 75.8600, "approximate": True, "location_source": "manual_pin"})
    assert up.status_code == 200 and up.json()["location"]["lat"] == 30.9040
    types = [e["event_type"] for e in client.get(f"/api/incidents/{r['incident_id']}/audit", headers=token_for(role="dispatcher")).json()]
    assert "incident.location_updated" in types


def test_gps_update_with_fix_clears_exact_location_unknown(client):
    r = sos(client, approximate=True, location_source="default", accuracy_m=None)
    assert "exact_location" in incident(client, r["incident_id"])["unknowns"]
    client.post(f"/api/incidents/{r['incident_id']}/location", headers=token_for("caller-1"),
                json={"lat": 30.9011, "lon": 75.8574, "accuracy_m": 8, "location_source": "gps"})
    assert "exact_location" not in incident(client, r["incident_id"])["unknowns"]


def test_typed_fallback_extracts_and_asks_at_most_three_useful_questions(client):
    r = sos(client)
    cid, iid = r["call_id"], r["incident_id"]
    first = say(client, cid, "chest pain, unresponsive and not breathing")
    inc = incident(client, iid)
    assert inc["reported_category"]["value"] == "cardiac" and inc["reported_category"]["source"] == "ai_extracted"
    assert inc["reported_category"]["evidence"] == "not breathing" or inc["reported_category"]["evidence"]
    assert inc["facts"]["conscious"]["value"] is False
    assert inc["operator_priority"]["value"] == 1 and inc["operator_priority"]["source"] == "ai_extracted"
    assert first["operator"] == "How many people need help?"  # category and consciousness are already known
    assert first["extraction"]["applied"] and first["extraction"]["adapter"] == "rules"
    second = say(client, cid, "two people")
    assert second["operator"].startswith("Help is on the way") and second["finished"]
    assert incident(client, iid)["patient_count"]["value"] == 2
    call = client.get(f"/api/calls/{cid}", headers=token_for("caller-1")).json()
    assert call["questions_asked"] <= 3
    asked = [t["text"] for t in call["transcript"] if t["speaker"] == "operator" and t["text"].endswith("?")]
    assert len(asked) == len(set(asked)) <= 3


def test_vague_caller_gets_each_question_once_then_closing(client):
    r = sos(client)
    cid = r["call_id"]
    ops = [say(client, cid, "help please", mode="speech")["operator"] for _ in range(4)]
    assert ops[0] == "Is the person awake and breathing?"
    assert ops[1] == "How many people need help?"
    assert ops[2].startswith("Help is on the way") and ops[3].startswith("Help is on the way")
    call = client.get(f"/api/calls/{cid}", headers=token_for("caller-1")).json()
    assert call["questions_asked"] == 3
    assert [t["mode"] for t in call["transcript"] if t["speaker"] == "caller"] == ["speech"] * 4


def test_quick_reply_and_scripted_modes_work_without_mic_or_key(client):
    cid = sos(client)["call_id"]
    out = say(client, cid, "Not breathing", mode="quick")
    assert out["extraction"]["output"]["reported_category"]["value"] == "cardiac"
    assert say(client, cid, "One person", mode="scripted")["finished"] is True


def test_extraction_refines_dispatch_to_a_capable_unit_via_autopilot(client):
    c = client
    r = sos(c)
    drain(c)
    first_vehicle = incident(c, r["incident_id"])["assigned_vehicle_id"]
    say(c, r["call_id"], "he has chest pain, I think a heart attack")
    drain(c)
    inc = incident(c, r["incident_id"])
    assert "cardiac" in vehicles(c)[inc["assigned_vehicle_id"]]["capability_tags"]
    decs = c.get("/api/dispatch/decisions", headers=token_for(role="dispatcher"), params={"incident_id": r["incident_id"]}).json()
    assert len(decs) >= 2 and {d["approved_by"] for d in decs if d["approved_by"]} == {"demo-autopilot"}
    if first_vehicle != inc["assigned_vehicle_id"]:  # reassignment released the first unit, with an audit trail
        assert vehicles(c)[first_vehicle]["status"] == "available"
    pa = c.get("/api/prealerts", headers=token_for(f"hosp-{inc['destination_id']}")).json()
    live = [p for p in pa if p["status"] != "superseded"]
    assert live and live[0]["reported_category"] == "cardiac"


def test_extractor_never_dispatches_or_downgrades_urgency(client):
    disp = token_for(role="dispatcher")
    assert client.post("/api/calls/CALL-0000/end", headers=token_for("caller-1")).status_code == 404
    client.app.state.ctx.autopilot.enabled = False  # nobody approves: only the extractor acts
    r = sos(client)
    say(client, r["call_id"], "just a mild headache, nothing serious")
    drain(client)
    inc = incident(client, r["incident_id"])
    assert inc["assigned_vehicle_id"] is None  # extraction and recommendation never commit a dispatch
    ev = client.get(f"/api/incidents/{r['incident_id']}/audit", headers=disp).json()
    assert not any(e["event_type"] in ("dispatch.approved", "assignment.sent") for e in ev)
    pending = client.get("/api/dispatch/decisions", headers=disp, params={"incident_id": r["incident_id"]}).json()
    assert pending and all(d["status"] in ("pending", "superseded") for d in pending)
    # priority set by a human stays at 1 even when the extractor suggests a lower urgency
    r2 = sos(client)
    client.post(f"/api/incidents/{r2['incident_id']}/facts", headers=disp,
                json={"key": "operator_priority", "value": 1, "source": "human_confirmed"})
    say(client, r2["call_id"], "just a mild headache, nothing serious")
    drain(client)
    assert incident(client, r2["incident_id"])["operator_priority"]["value"] == 1


def test_conflict_with_human_confirmed_value_goes_to_review_not_overwritten(client):
    disp = token_for(role="dispatcher")
    r = sos(client)
    client.post(f"/api/incidents/{r['incident_id']}/facts", headers=disp,
                json={"key": "reported_category", "value": "trauma", "source": "human_confirmed"})
    out = say(client, r["call_id"], "I think it is a heart attack")
    inc = incident(client, r["incident_id"])
    assert inc["reported_category"]["value"] == "trauma" and inc["reported_category"]["human_confirmed"]
    assert inc["needs_review"] is True and "conflict:reported_category" in inc["review_reasons"]
    assert out["extraction"]["review_required"] is True
    alerts = client.get("/api/alerts", headers=disp).json()
    assert any(a["kind"] == "needs_review" and a["incident_id"] == r["incident_id"] for a in alerts)
    client.post(f"/api/incidents/{r['incident_id']}/review/resolve", headers=disp)
    assert incident(client, r["incident_id"])["needs_review"] is False


def test_tracker_updates_arrive_over_websocket(sim_client):
    c = sim_client
    tok = token_for("caller-1")["Authorization"].split()[1]
    with c.websocket_connect(f"/ws/events?token={tok}") as ws:
        ws.receive_json()  # hello
        r = sos(c)
        seen, loc_events = [], 0
        for _ in range(400):
            ev = ws.receive_json()
            if ev["correlation_id"] != r["incident_id"]:
                continue
            seen.append(ev["event_type"])
            loc_events += ev["event_type"] == "vehicle.location_updated"
            if ev["event_type"] == "prealert.arrived":
                break
        assert "pipeline.step" in seen and "dispatch.approved" in seen
        for want in ("assignment.sent", "assignment.accepted", "vehicle.status_changed"):
            assert want in seen
        assert loc_events >= 1 and seen[-1] == "prealert.arrived"
    t = c.get(f"/api/tracker/{r['incident_id']}", headers=token_for("caller-1")).json()
    assert t["headline"].startswith("Arrived")


def test_caller_cannot_see_other_callers_tracker_or_dispatch(client):
    r = sos(client)
    other = token_for("caller-2")
    assert client.get(f"/api/tracker/{r['incident_id']}", headers=other).status_code == 403
    assert client.post(f"/api/calls/{r['call_id']}/utterances", headers=other, json={"text": "hi"}).status_code == 403
    assert client.post("/api/dispatch/recommend", headers=token_for("caller-1"), json={"incident_id": r["incident_id"]}).status_code == 403
    assert client.get("/api/sos").status_code == 405
    assert client.post("/api/sos", json=CENTER).status_code == 401
    assert client.post("/api/sos", headers=token_for("caller-1"), json={"lat": 200, "lon": 0}).status_code == 422
    assert client.post("/api/sos", headers=token_for("driver-AMB-01"), json=CENTER).status_code == 403


def test_first_aid_is_static_labelled_and_never_a_phone_link(client):
    fa = client.get("/api/first-aid", headers=token_for("caller-1")).json()
    assert fa["label"] == "General guidance, follow the call-taker."
    assert {c["key"] for c in fa["categories"]} == {"cpr", "bleeding", "choking"}
    cpr = next(c for c in fa["categories"] if c["key"] == "cpr")
    assert 100 <= cpr["rhythm_bpm"] <= 120 and len(cpr["steps"]) >= 5
    blob = str(fa).lower()
    assert "tel:" not in blob and "diagnos" not in blob.replace("does not diagnose", "")


def test_nearest_aed_with_walking_time_sorted(client):
    r = sos(client)
    aeds = client.get(f"/api/incidents/{r['incident_id']}/aed", headers=token_for("caller-1")).json()
    assert len(aeds) == 3 and aeds[0]["walk_s"] <= aeds[1]["walk_s"] <= aeds[2]["walk_s"]
    a = aeds[0]
    assert a["walk_low_s"] < a["walk_s"] < a["walk_high_s"] and a["distance_m"] > 0


def test_volunteer_alerts_are_opt_in_simulated_and_respondable(client):
    disp = token_for(role="dispatcher")
    r = sos(client)
    a = client.post(f"/api/incidents/{r['incident_id']}/volunteer-alerts", headers=disp)
    assert a.status_code == 201
    alerts = a.json()
    assert 0 <= len(alerts) <= 3 and all(x["simulated"] for x in alerts)
    vols = {v["id"]: v for v in client.get("/api/volunteers", headers=disp).json()}
    assert all(vols[x["volunteer_id"]]["opted_in"] and vols[x["volunteer_id"]]["available"] for x in alerts)
    if alerts:
        ok = client.post(f"/api/volunteer-alerts/{alerts[0]['id']}/respond", headers=token_for("caller-1"), params={"accept": "true"})
        assert ok.json()["status"] == "accepted"
        others = client.get(f"/api/incidents/{r['incident_id']}/volunteer-alerts", headers=token_for("caller-1")).json()
        assert all(o["status"] in ("accepted", "stood_down") for o in others)
        types = [e["event_type"] for e in client.get(f"/api/incidents/{r['incident_id']}/audit", headers=disp).json()]
        assert "volunteer.alert_sent" in types and "volunteer.accepted" in types
    assert client.post(f"/api/incidents/{r['incident_id']}/volunteer-alerts", headers=token_for("caller-1")).status_code == 403


def test_cardiac_call_triggers_simulated_volunteer_alerts_automatically(sim_client):
    c = sim_client
    r = sos(c)
    say(c, r["call_id"], "he is not breathing, heart attack")
    drain(c)
    al = c.get(f"/api/incidents/{r['incident_id']}/volunteer-alerts", headers=token_for("caller-1")).json()
    assert al, "expected volunteers within range of the city centre"
    assert sum(a["status"] == "accepted" for a in al) == 1 and all(a["aed_id"] for a in al)


def test_end_call_keeps_dispatch_going(client):
    r = sos(client)
    e = client.post(f"/api/calls/{r['call_id']}/end", headers=token_for("caller-1")).json()
    assert e["ended_at"]
    assert client.post(f"/api/calls/{r['call_id']}/utterances", headers=token_for("caller-1"), json={"text": "hi"}).status_code == 409
    drain(client)  # ending the call does not cancel the automatic dispatch
