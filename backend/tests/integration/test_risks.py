from datetime import timedelta

import pytest

from tests.conftest import token_for
from tests.integration.test_driver_hospital_flow import DISP, drv, hosp, make_assigned, now_iso, set_status

KINDS = {"keep_current_plan", "request_confirmation", "reroute", "alternate_destination", "propose_eligible_unit", "escalate"}


def evaluate(c):
    r = c.post("/api/risks/evaluate", headers=DISP)
    assert r.status_code == 200, r.text
    return r.json()


def risks(c, **params):
    return c.get("/api/risks", headers=DISP, params=params).json()


def of_type(c, t, incident_id=None):
    return [r for r in risks(c, **({"incident_id": incident_id} if incident_id else {})) if r["type"] == t]


def recover(c, rid, kind, reason="reviewed and approved", who=None):
    return c.post(f"/api/risks/{rid}/recover", headers=who or DISP, json={"option_kind": kind, "reason": reason})


def opt(r, kind):
    return next((o for o in r["options"] if o["kind"] == kind), None)


def age_vehicle(c, run, vid, seconds):
    ctx = c.app.state.ctx

    def fn(v):
        t = ctx.now() - timedelta(seconds=seconds)
        v.location_at = v.last_update_at = t
        return v

    run(ctx.vehicle_repo.modify, vid, fn)


def age_hospital(c, run, hid, seconds):
    ctx = c.app.state.ctx

    def fn(h):
        for r in h.resources.values():
            r.last_confirmed_at = ctx.now() - timedelta(seconds=seconds)
        return h

    run(ctx.hospital_repo.modify, hid, fn)


@pytest.fixture
def manual(client):
    """No autopilot: every approval in these tests is a human one."""
    client.app.state.ctx.autopilot.enabled = False
    return client


def start(c, accept=True):
    iid, vid, hid, _ = make_assigned(c)
    if accept:
        a = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()[0]
        c.post(f"/api/assignments/{a['id']}/accept", headers=drv(vid))
    return iid, vid, hid


def check_shape(r):
    assert r["evidence"] and r["affected_assumption"] and r["severity"] in ("low", "medium", "high", "critical")
    kinds = [o["kind"] for o in r["options"]]
    assert "keep_current_plan" in kinds  # always present
    assert set(kinds) <= KINDS and sum(o["recommended"] for o in r["options"]) == 1
    for o in r["options"]:
        assert {"time_delta_s", "suitability", "coverage_delta", "other_incidents"} <= set(o["effects"])


def audit_types(c, iid):
    return [e["event_type"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()]


# ---- five+ failure types each produce an auditable proposal that needs approval -------------------
def test_stale_vehicle_gps_proposes_confirmation_and_changes_nothing(manual, run):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    new = evaluate(c)
    r = of_type(c, "stale_vehicle_gps", iid)[0]
    assert r["id"] in [x["id"] for x in new] and r["entity"] == vid and r["severity"] == "critical"
    check_shape(r)
    assert r["evidence"][0]["key"] == "location_age_s" and r["evidence"][0]["value"] >= 300
    assert opt(r, "request_confirmation")["recommended"]  # cheap and reversible first
    assert opt(r, "propose_eligible_unit") is not None
    # nothing moved: the unit is still assigned until a human approves
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == vid
    assert "risk.detected" in [e["event_type"] for e in c.get("/api/events", headers=DISP, params={"entity_id": r["id"]}).json()]


def test_no_acknowledgment_recommends_next_unit_and_only_a_human_applies_it(manual):
    c = manual
    ctx = c.app.state.ctx
    iid, vid, hid = start(c, accept=False)
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.ack_timeout_s + 5)
    evaluate(c)
    r = of_type(c, "no_acknowledgment", iid)[0]
    check_shape(r)
    best = opt(r, "propose_eligible_unit")
    assert best["recommended"] and best["necessary"] and best["effects"]["time_delta_s"] is None or best["recommended"]
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == vid  # proposal only
    # a dispatcher approves: audited release and new assignment, with the reason
    done = recover(c, r["id"], "propose_eligible_unit", "crew silent for over a minute")
    assert done.status_code == 200, done.text
    inc = c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]
    assert inc["assigned_vehicle_id"] not in (None, vid)
    vs = {v["id"]: v for v in c.get("/api/vehicles", headers=DISP).json()}
    assert vs[vid]["status"] == "available"
    types = audit_types(c, iid)
    assert "risk.recovery_approved" in types and "dispatch.overridden" in types
    rec = next(e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json() if e["event_type"] == "risk.recovery_approved")
    assert rec["actor"] == "disp-1" and rec["reason"] == "crew silent for over a minute"


def test_a_closure_the_policy_already_routed_around_raises_no_closure_risk(manual):
    """M20: a closed road is handled at once by the stable-route policy (same destination, driver acknowledges), so the risk
    monitor finds nothing left to propose."""
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    pl = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()["route"]["polyline"]
    mid = pl[int(len(pl) * 0.6)]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 250})
    evaluate(c)
    assert of_type(c, "route_closure", iid) == []
    assert c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()["route_notice"]["reason"] == "blocked"


def test_route_closure_offers_reroute_that_the_driver_must_still_accept(manual):
    """The human-approved mode (stable-route policy off): the risk monitor proposes, a dispatcher approves, the driver taps."""
    c = manual
    assert c.post("/api/routing/policy", headers=token_for(role="admin"), json={"enabled": False}).json()["enabled"] is False
    assert c.post("/api/routing/policy", headers=DISP, json={"enabled": True}).status_code == 403       # admins only
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    pl = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()["route"]["polyline"]
    mid = pl[int(len(pl) * 0.6)]
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 250})
    evaluate(c)
    r = of_type(c, "route_closure", iid)[0]
    check_shape(r)
    ro = opt(r, "reroute")
    assert ro["recommended"] and ro["necessary"] and r["evidence"][0]["key"] == "closures"
    done = recover(c, r["id"], "reroute", "road closed ahead")
    assert done.json()["status"] == "awaiting"
    plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
    assert plan["reroute"]["status"] == "pending"  # approved by the dispatcher, applied only on the driver's tap
    assert c.post(f"/api/driver/{vid}/reroute/{plan['reroute']['id']}/accept", headers=drv(vid)).status_code == 200
    evaluate(c)  # the condition is gone: the risk clears on its own
    assert c.get(f"/api/risks/{r['id']}", headers=DISP).json()["status"] == "cleared"


def test_stale_hospital_resource_asks_the_hospital_and_clears_when_it_answers(manual, run):
    c = manual
    iid, vid, hid = start(c)
    age_hospital(c, run, hid, 7200)
    evaluate(c)
    r = of_type(c, "stale_hospital_resource", iid)[0]
    check_shape(r)
    assert opt(r, "request_confirmation")["recommended"]
    assert r["evidence"][0]["key"] == "resource"
    done = recover(c, r["id"], "request_confirmation", "ask the cath lab to confirm")
    got = done.json()
    assert got["status"] == "awaiting" and got["confirmations"][0]["target_id"] == hid
    res = got["confirmations"][0]["resource"]
    c.patch(f"/api/hospitals/{hid}/resources", headers=hosp(hid), json={
        "update_id": "cf1", "occurred_at": now_iso(c), "resource": res, "status": "available", "count": 1})
    evaluate(c)
    assert c.get(f"/api/risks/{r['id']}", headers=DISP).json()["status"] == "cleared"


def test_capacity_change_requires_an_alternate_destination_and_supersedes_the_prealert(manual):
    c = manual
    iid, vid, hid = start(c)
    c.patch(f"/api/hospitals/{hid}/resources", headers=hosp(hid), json={
        "update_id": "cap1", "occurred_at": now_iso(c, 1), "resource": "cath_lab", "status": "unavailable", "source": "human_confirmed"})
    evaluate(c)
    r = of_type(c, "capacity_change", iid)[0]
    check_shape(r)
    alt = opt(r, "alternate_destination")
    assert alt and alt["necessary"] and alt["recommended"] and alt["payload"]["hospital_id"] != hid
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["destination_id"] == hid  # not yet
    done = recover(c, r["id"], "alternate_destination", "cath lab went down")
    assert done.status_code == 200, done.text
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["destination_id"] == alt["payload"]["hospital_id"]
    old = c.get("/api/prealerts", headers=hosp(hid)).json()
    assert old[0]["status"] == "superseded"  # redirected openly, never deleted
    assert "prealert.superseded" in audit_types(c, iid)


def test_eta_drift_hysteresis_does_not_flap_under_noisy_eta(manual):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    ids, props = set(), set()
    # noisy congestion: 1.4x, 1.18x, 1.4x ... straddles the raise (1.25) and clear (1.10) thresholds
    for mult in ([1.4, 1.18, 1.4, 1.18, 1.4, 1.18, 1.4, 1.18]):
        for d in c.get("/api/routing/disruptions", headers=DISP).json():
            c.delete(f"/api/routing/disruptions/{d['id']}", headers=DISP)
        c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "multiplier": mult})
        evaluate(c)
        for r in of_type(c, "eta_drift", iid):
            ids.add(r["id"])
            assert r["status"] in ("open", "awaiting")
        plan = c.get(f"/api/driver/{vid}/plan", headers=drv(vid)).json()
        if plan["reroute"]:
            props.add(plan["reroute"]["id"])
    assert len(ids) == 1  # one risk, raised once and held (not re-raised each wobble)
    assert len(props) <= 1  # and at most one reroute proposal for the driver
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == vid  # plan never flipped
    r = of_type(c, "eta_drift", iid)[0]
    assert r["seen_count"] >= 4
    # once the congestion is really gone the risk clears
    for d in c.get("/api/routing/disruptions", headers=DISP).json():
        c.delete(f"/api/routing/disruptions/{d['id']}", headers=DISP)
    evaluate(c)
    assert c.get(f"/api/risks/{r['id']}", headers=DISP).json()["status"] == "cleared"


def test_state_conflict_when_records_disagree(manual):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    c.patch(f"/api/vehicles/{vid}/state", headers=DISP, json={
        "update_id": "conf1", "occurred_at": now_iso(c, 1), "status": "available", "reason": "stale tablet"})
    evaluate(c)
    r = of_type(c, "state_conflict", iid)[0]
    check_shape(r)
    assert any("vehicle reports available" in str(e["value"]) for e in r["evidence"])
    assert opt(r, "request_confirmation")["recommended"]


def test_competing_incident_hands_a_unit_over_only_with_approval(manual):
    c = manual
    # a low-priority call holds a cardiac-capable unit; every other cardiac unit is out of service
    iid_low = c.post("/api/incidents", headers=DISP, json={
        "reported_category": "cardiac", "operator_priority": 3, "location": {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 10},
        "text": "mild chest tightness"}).json()["id"]
    d = c.post("/api/dispatch/recommend", headers=DISP, json={"incident_id": iid_low, "autopilot": False}).json()
    c.post(f"/api/dispatch/{d['id']}/approve", headers=DISP, json={"action": "approve", "reason": "looks right"})
    held = d["recommendation"]["vehicle_id"]
    for v in c.get("/api/vehicles", headers=DISP).json():
        if v["id"] != held and v["status"] == "available" and {"ALS", "cardiac"} <= set(v["capability_tags"]):
            c.patch(f"/api/vehicles/{v['id']}/state", headers=DISP, json={
                "update_id": f"oos-{v['id']}", "occurred_at": now_iso(c, 1), "status": "unavailable", "reason": "test"})
    iid_hi = c.post("/api/incidents", headers=DISP, json={
        "reported_category": "cardiac", "operator_priority": 1, "location": {"lat": 30.9020, "lon": 75.8580, "accuracy_m": 10},
        "text": "cardiac arrest"}).json()["id"]
    evaluate(c)
    r = of_type(c, "competing_incident", iid_low)[0]
    check_shape(r)
    assert r["entity"] == held and {"key": "competing_incident", "value": iid_hi} == {k: e[k] for e in r["evidence"] if e["key"] == "competing_incident" for k in ("key", "value")}
    hand = opt(r, "propose_eligible_unit")
    assert hand["recommended"] and hand["effects"]["other_incidents"][0]["incident_id"] == iid_low
    assert c.get(f"/api/incidents/{iid_low}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == held  # untouched so far
    done = recover(c, r["id"], "propose_eligible_unit", "cardiac arrest outranks mild chest tightness")
    assert done.status_code == 200, done.text
    low = c.get(f"/api/incidents/{iid_low}", headers=DISP).json()["incident"]
    assert low["assigned_vehicle_id"] is None  # released with an audit trail, not cancelled silently
    assert "incident.unit_released_for_competing" in audit_types(c, iid_low)


# ---- planner rules --------------------------------------------------------------------------------------------------
def test_dedupes_alerts_and_counts_sightings(manual, run):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    first = evaluate(c)
    again = evaluate(c)
    assert len(first) >= 1 and again == []
    mine = of_type(c, "stale_vehicle_gps", iid)
    assert len(mine) == 1 and mine[0]["seen_count"] >= 2
    types = [e["event_type"] for e in c.get("/api/events", headers=DISP, params={"entity_id": mine[0]["id"]}).json()]
    assert types.count("risk.detected") == 1


def test_keep_current_plan_resolves_then_cooldown_blocks_re_raising_until_it_expires(manual, run):
    c = manual
    ctx = c.app.state.ctx
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    evaluate(c)
    r = of_type(c, "stale_vehicle_gps", iid)[0]
    done = recover(c, r["id"], "keep_current_plan", "crew radioed that they are fine").json()
    assert done["status"] == "resolved" and done["resolution"]["option"] == "keep_current_plan"
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == vid
    assert evaluate(c) == []  # cooldown: no new alert for the same thing
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.risk_cooldown_s + 1)
    age_vehicle(c, run, vid, 400)
    assert [x["type"] for x in evaluate(c)].count("stale_vehicle_gps") == 1


def test_cannot_apply_unknown_or_closed_options_and_roles_are_enforced(manual, run):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    evaluate(c)
    r = of_type(c, "stale_vehicle_gps", iid)[0]
    assert recover(c, r["id"], "alternate_destination").status_code == 409  # not an option for this risk
    assert c.post(f"/api/risks/{r['id']}/recover", headers=DISP, json={"option_kind": "keep_current_plan", "reason": "x"}).status_code == 422
    for who in (token_for("caller-1"), drv(vid), hosp(hid)):
        assert recover(c, r["id"], "keep_current_plan", who=who).status_code == 403
        assert c.get("/api/risks", headers=who).status_code == 403
    assert recover(c, r["id"], "keep_current_plan").status_code == 200
    assert recover(c, r["id"], "keep_current_plan").status_code == 409  # already resolved


def test_escalate_is_simulated_and_changes_nothing(manual, run):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    evaluate(c)
    r = of_type(c, "stale_vehicle_gps", iid)[0]
    out = recover(c, r["id"], "escalate", "supervisor to call the crew").json()
    assert out["escalated"] is True and out["status"] == "open" and "No real call" in out["resolution"]["message"]
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] == vid


def test_demo_autopilot_may_approve_a_recommended_change_and_is_audited(client, run):
    c = client
    ctx = c.app.state.ctx
    ctx.autopilot.enabled = False
    iid, vid, hid = start(c, accept=False)
    ctx.autopilot.enabled = True
    ctx.settings.risk_autopilot_delay_s = 0
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.ack_timeout_s + 5)
    evaluate(c)
    run(ctx.drain)
    r = of_type(c, "no_acknowledgment", iid)[0]
    assert r["status"] == "resolved" and r["resolution"]["by"] == "demo-autopilot"
    ev = [e for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json() if e["event_type"] == "risk.recovery_approved"][0]
    assert ev["actor"] == "demo-autopilot" and ev["payload"]["autopilot"] is True and "Demo Autopilot" in ev["reason"]
    assert c.get(f"/api/incidents/{iid}", headers=DISP).json()["incident"]["assigned_vehicle_id"] != vid


def test_autopilot_leaves_keep_and_escalate_to_a_human(client, run):
    c = client
    ctx = c.app.state.ctx
    ctx.autopilot.enabled = False
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    ctx.autopilot.enabled = True
    ctx.settings.risk_autopilot_delay_s = 0
    c.post("/api/routing/disruptions", headers=DISP, json={"kind": "congestion", "multiplier": 1.4})
    evaluate(c)
    run(ctx.drain)
    r = of_type(c, "eta_drift", iid)
    if r:  # recommended option for plain drift with no better route is "keep": a human decides
        assert r[0]["status"] == "open" or r[0]["resolution"]["by"] != "demo-autopilot" or r[0]["resolution"]["option"] != "keep_current_plan"


def test_open_risks_appear_in_the_alerts_strip(manual, run):
    c = manual
    iid, vid, hid = start(c)
    set_status(c, vid, "en_route", 1)
    age_vehicle(c, run, vid, 300)
    evaluate(c)
    alerts = c.get("/api/alerts", headers=DISP).json()
    assert any(a["kind"] == "risk" and a["incident_id"] == iid for a in alerts)
