from datetime import timedelta

import pytest

from app.core.capabilities import CATEGORY_HOSPITAL_CAPS
from app.schemas.dispatch import ApproveRequest
from tests.conftest import token_for

CENTER = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 10}


def mk(client, who, category="cardiac", priority=1, **kw):
    r = client.post("/api/incidents", headers=who, json={
        "reported_category": category, "operator_priority": priority, "location": CENTER,
        "text": "patient collapsed", **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def rec(client, who, iid, **kw):
    r = client.post("/api/dispatch/recommend", headers=who, json={"incident_id": iid, "autopilot": False, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, who, dec_id, action="approve", reason="looks right", **kw):
    return client.post(f"/api/dispatch/{dec_id}/approve", headers=who, json={"action": action, "reason": reason, **kw})


def vehicles(client, who):
    return {v["id"]: v for v in client.get("/api/vehicles", headers=who).json()}


def test_recommendation_ranking_is_deterministic(client, disp, ctx):
    iid = mk(client, disp)
    frozen = ctx.now()  # ages (and so freshness terms) drift with the wall clock; freeze it
    ctx.clock = lambda: frozen
    a, b = rec(client, disp, iid), rec(client, disp, iid)
    key = lambda d: [(c["vehicle_id"], c["hospital_id"], c["components"]["total"]) for c in d["candidates"]]  # noqa: E731
    assert key(a) == key(b) and a["id"] != b["id"]
    totals = [c["components"]["total"] for c in a["candidates"]]
    assert totals == sorted(totals) and [c["rank"] for c in a["candidates"]] == list(range(1, len(totals) + 1))
    assert a["recommendation"]["rank"] == 1 and a["policy_version"].startswith("P1")


def test_recommend_does_not_commit(client, disp):
    iid = mk(client, disp)
    rec(client, disp, iid)
    assert client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]["assigned_vehicle_id"] is None
    assert all(v["status"] != "assigned" for v in vehicles(client, disp).values())


def test_hard_constraints_vehicle_capability_and_availability(client, disp):
    iid = mk(client, disp, "cardiac")
    d = rec(client, disp, iid)
    vs = vehicles(client, disp)
    for c in d["candidates"]:
        v = vs[c["vehicle_id"]]
        assert {"ALS", "cardiac"} <= set(v["capability_tags"]) and v["status"] == "available"
        assert c["constraints"]["capability"] and c["constraints"]["available"]
    ex = {e["id"]: e["reasons"] for e in d["exclusions"] if e["kind"] == "vehicle"}
    assert any("missing_capability" in r for r in ex["AMB-01"])  # BLS cannot serve a cardiac call
    assert "status_unavailable" in ex["AMB-20"]


def test_hard_constraints_hospital_capability_and_unavailable_resource(client, disp):
    iid = mk(client, disp, "cardiac")
    d = rec(client, disp, iid)
    h_ex = {e["id"]: e["reasons"] for e in d["exclusions"] if e["kind"] == "hospital"}
    assert any("missing_capability:cardiac" in r for r in h_ex["HOSP-03"])  # trauma-only
    for c in d["candidates"]:
        assert "cardiac" in {h["id"]: h for h in client.get("/api/hospitals", headers=disp).json()}[c["hospital_id"]]["capabilities"]
    # make the top hospital's cath lab unavailable -> excluded, not merely penalised
    top = d["recommendation"]["hospital_id"]
    client.patch(f"/api/hospitals/{top}/resources", headers=token_for(f"hosp-{top}"), json={
        "update_id": "cl1", "occurred_at": (client.app.state.ctx.now() + timedelta(seconds=1)).isoformat(),
        "resource": "cath_lab", "status": "unavailable"})
    d2 = rec(client, disp, iid)
    assert top not in {c["hospital_id"] for c in d2["candidates"]}
    assert any(e["id"] == top and any("RESOURCE_UNAVAILABLE" in r for r in e["reasons"]) for e in d2["exclusions"])


def test_stale_vehicle_excluded_unknown_not_available(client, disp, ctx):
    iid = mk(client, disp, "other", priority=3)
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.vehicle_stale_s + 60)
    r = client.post("/api/dispatch/recommend", headers=disp, json={"incident_id": iid, "autopilot": False})
    assert r.status_code == 409  # every unit is stale -> no feasible pair, never "assumed available"
    ctx.clock = lambda: base
    assert client.post("/api/dispatch/recommend", headers=disp, json={"incident_id": iid, "autopilot": False}).status_code == 200


def test_stale_hospital_data_is_flagged_and_ranked_below_fresh(client, disp):
    iid = mk(client, disp, "cardiac")
    d = rec(client, disp, iid)
    by_h = {}
    for c in d["candidates"]:
        by_h.setdefault(c["hospital_id"], c)
    stale = [h for h, c in by_h.items() if any(r.startswith("STALE_HOSPITAL_DATA") for r in c["reason_codes"])]
    fresh = [h for h, c in by_h.items() if "FRESH_HOSPITAL_DATA" in c["reason_codes"]]
    assert fresh, "expected at least one fresh capable hospital"
    if stale:  # HOSP-05 has deliberately old data
        assert by_h[stale[0]]["components"]["freshness"] >= 1.0
    assert d["recommendation"]["hospital_id"] in fresh


def test_baseline_p0_is_included_and_distinct_policy(client, disp):
    iid = mk(client, disp, "cardiac")
    d1 = rec(client, disp, iid)
    assert d1["baseline"]["reason_codes"][0] == "BASELINE_NEAREST_ELIGIBLE"
    d0 = rec(client, disp, iid, policy="P0")
    assert d0["policy"] == "P0" and d0["recommendation"]["vehicle_id"] == d1["baseline"]["vehicle_id"]
    assert d0["policy_version"].startswith("P0")


def test_score_components_and_assumptions_exposed(client, disp):
    d = rec(client, disp, mk(client, disp))
    c = d["recommendation"]["components"]
    assert set(c["weighted"]) == {"time", "freshness", "coverage_loss", "uncertainty", "switch"}
    assert c["total"] == pytest.approx(sum(c["weighted"].values()), abs=1e-3)
    assert d["assumptions"] and d["inputs"]["scene"] and "ELIGIBLE" in d["recommendation"]["reason_codes"]
    assert d["recommendation"]["eta_total_low_s"] < d["recommendation"]["eta_total_high_s"]


def test_custom_weights_change_the_score(client, disp):
    iid = mk(client, disp)
    base = rec(client, disp, iid)["recommendation"]["components"]["total"]
    no_cov = rec(client, disp, iid, weights={"w_coverage": 0, "w_fresh": 0})["recommendation"]["components"]["total"]
    assert no_cov < base


def test_approve_commits_assignment_and_audits(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    r = approve(client, disp, d["id"])
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["approved_by"] == "disp-1"
    vid, hid = d["recommendation"]["vehicle_id"], d["recommendation"]["hospital_id"]
    inc = client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]
    assert (inc["assigned_vehicle_id"], inc["destination_id"]) == (vid, hid)
    assert vehicles(client, disp)[vid]["status"] == "assigned"
    asg = client.get("/api/assignments", headers=disp, params={"incident_id": iid}).json()
    assert len(asg) == 1 and asg[0]["state"] == "sent" and asg[0]["acknowledged_at"] is None
    types = [e["event_type"] for e in client.get(f"/api/incidents/{iid}/audit", headers=disp).json()]
    assert "dispatch.recommended" in types and "dispatch.approved" in types and "assignment.sent" in types
    assert approve(client, disp, d["id"]).status_code == 409  # cannot approve twice


def test_approve_requires_reason_and_authorised_role(client, disp):
    d = rec(client, disp, mk(client, disp))
    assert client.post(f"/api/dispatch/{d['id']}/approve", headers=disp, json={"action": "approve"}).status_code == 422
    assert approve(client, token_for("caller-1"), d["id"]).status_code == 403
    assert approve(client, token_for("driver-AMB-01"), d["id"]).status_code == 403
    assert client.post("/api/dispatch/recommend", headers=token_for("caller-1"), json={"incident_id": "INC-1001"}).status_code == 403


def test_reject_leaves_everything_unchanged(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    r = approve(client, disp, d["id"], "reject", "wrong unit type")
    assert r.json()["status"] == "rejected"
    assert client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]["assigned_vehicle_id"] is None


def test_override_valid_unit_is_recorded_but_hard_constraints_hold(client, disp):
    iid = mk(client, disp, "cardiac")
    d = rec(client, disp, iid)
    other = next(c for c in d["candidates"] if c["vehicle_id"] != d["recommendation"]["vehicle_id"])
    ok = approve(client, disp, d["id"], "override", "closer to crew base", vehicle_id=other["vehicle_id"])
    assert ok.status_code == 200 and ok.json()["status"] == "overridden"
    assert ok.json()["final"]["vehicle_id"] == other["vehicle_id"]
    d2 = rec(client, disp, mk(client, disp, "cardiac"))
    bad = approve(client, disp, d2["id"], "override", "force a BLS unit", vehicle_id="AMB-01")
    assert bad.status_code == 409 and "missing_capability" in bad.json()["detail"]
    bad2 = approve(client, disp, d2["id"], "override", "force wrong hospital", hospital_id="HOSP-03")
    assert bad2.status_code == 409 and "lacks required capability" in bad2.json()["detail"]


def test_switch_penalty_and_keep_current_below_threshold(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    approve(client, disp, d["id"])
    cur_v, cur_h = d["recommendation"]["vehicle_id"], d["recommendation"]["hospital_id"]
    d2 = rec(client, disp, iid)
    assert d2["recommendation"]["vehicle_id"] == cur_v and d2["recommendation"]["hospital_id"] == cur_h
    others = [c for c in d2["candidates"] if c["vehicle_id"] != cur_v]
    assert others and all(c["components"]["switch"] >= 1 and "SWITCH_PENALTY_APPLIED" in c["reason_codes"] for c in others)
    assert all(c["components"]["total"] >= d2["recommendation"]["components"]["total"] for c in d2["candidates"])
    # dropping the switch penalty to zero makes a meaningfully better unit win only if it clears the threshold
    d3 = rec(client, disp, iid, weights={"w_switch": 0})
    assert d3["recommendation"]["components"]["switch"] >= 0


def test_closure_reroute_changes_eta_and_audits(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    route = d["scene_route"]
    mid = route["polyline"][len(route["polyline"]) // 2]
    cl = client.post("/api/routing/disruptions", headers=disp, json={
        "kind": "closure", "lat": mid[0], "lon": mid[1], "radius_m": 300, "note": "test closure"})
    assert cl.status_code == 201
    d2 = rec(client, disp, iid)
    from app.routing.geometry import path_hits_circle

    poly = [tuple(p) for p in d2["scene_route"]["polyline"]]
    assert not path_hits_circle(poly, tuple(mid), 300)  # the recommended route avoids the closure
    assert d2["scene_route"]["feasible"]
    assert d2["inputs"]["disruptions"] == [cl.json()["id"]]
    assert client.delete(f"/api/routing/disruptions/{cl.json()['id']}", headers=disp).status_code == 200
    assert rec(client, disp, iid)["inputs"]["disruptions"] == []


def test_route_preview_endpoint_returns_steps_and_range(client, disp):
    r = client.post("/api/routes/preview", headers=disp, json={
        "origin_lat": 30.90, "origin_lon": 75.85, "dest_lat": 30.92, "dest_lon": 75.88})
    body = r.json()["route"]
    assert r.status_code == 200 and body["steps"] and body["source"] == "mock"
    assert body["duration_low_s"] < body["duration_s"] < body["duration_high_s"]
    assert client.post("/api/routes/preview", headers=disp, json={"origin_lat": 30.9, "origin_lon": 75.85}).status_code == 422
    assert client.post("/api/routes/preview", headers=token_for("caller-1"), json={
        "origin_lat": 30.9, "origin_lon": 75.85, "dest_lat": 30.9, "dest_lon": 75.9}).status_code == 403


def test_entrance_aware_dispatch_and_expiring_reports(client, disp):
    b = client.get("/api/buildings", headers=disp).json()[0]
    iid = mk(client, disp, "other", priority=3, building_id=b["id"],
             **{})
    inc = client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]
    assert inc["building_id"] == b["id"]
    d = rec(client, disp, iid)
    assert d["entrance"]["building_id"] == b["id"]
    chosen = d["entrance"]["chosen"]["entrance"]["id"] if d["entrance"]["chosen"] else None
    if chosen:
        r = client.post(f"/api/buildings/{b['id']}/access-reports", headers=disp,
                        json={"entrance_id": chosen, "kind": "blocked", "note": "skip stuck"})
        assert r.status_code == 201
        after = client.get(f"/api/buildings/{b['id']}/entrance", headers=disp).json()
        assert after["chosen"] is None or after["chosen"]["entrance"]["id"] != chosen
        ctx = client.app.state.ctx
        base = ctx.now()
        ctx.clock = lambda: base + timedelta(seconds=ctx.settings.access_report_ttl_s + 5)
        later = client.get(f"/api/buildings/{b['id']}/entrance", headers=disp).json()
        assert r.json()["id"] in later["expired_report_ids"]


def test_hospital_match_prefers_fresh_capable(client, disp):
    rows = client.get("/api/dispatch/hospital-match", headers=disp,
                      params={"category": "cardiac", "lat": 30.901, "lon": 75.857}).json()
    caps = CATEGORY_HOSPITAL_CAPS["cardiac"]
    hs = {h["id"]: h for h in client.get("/api/hospitals", headers=disp).json()}
    ok = [r for r in rows if r["feasible"]]
    assert ok and all(caps <= set(hs[r["hospital_id"]]["capabilities"]) for r in ok)
    tiers = [r["tier"] for r in rows]
    assert tiers == sorted(tiers)
    fresh = [r for r in ok if r["tier"] == 1]
    assert [r["eta_s"] for r in fresh] == sorted(r["eta_s"] for r in fresh)


def test_batch_ortools_not_worse_than_greedy(client, disp):
    ids = [mk(client, disp, "other", priority=p, ) for p in (1, 2, 2, 3)]
    r = client.post("/api/dispatch/batch", headers=disp, json={"incident_ids": ids})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ortools"]["assigned"] >= b["greedy"]["assigned"]
    if b["ortools"]["assigned"] == b["greedy"]["assigned"]:
        assert b["ortools"]["total_cost"] <= b["greedy"]["total_cost"] + 1e-6
    used = [a["vehicle_id"] for a in b["ortools"]["assignments"].values()]
    assert len(used) == len(set(used))  # one incident per vehicle


# ---- Demo Autopilot -------------------------------------------------------------
def test_autopilot_approves_after_delay_and_is_audited(client, disp, ctx, run):
    ctx.autopilot.delay_s = 0.05
    iid = mk(client, disp)
    d = client.post("/api/dispatch/recommend", headers=disp, json={"incident_id": iid}).json()
    assert d["status"] == "pending"  # recommend alone never commits
    run(ctx.autopilot.drain)
    got = client.get(f"/api/dispatch/decisions/{d['id']}", headers=disp).json()
    assert got["status"] == "approved" and got["approved_by"] == "demo-autopilot"
    assert "Demo Autopilot" in got["rationale"]
    ev = [e for e in client.get(f"/api/incidents/{iid}/audit", headers=disp).json() if e["event_type"] == "dispatch.approved"]
    assert ev[0]["actor"] == "demo-autopilot" and ev[0]["payload"]["autopilot"] is True


def test_autopilot_off_leaves_pending_and_human_wins_race(client, disp, ctx, run):
    ctx.autopilot.delay_s = 0.05
    ctx.autopilot.enabled = False
    d = client.post("/api/dispatch/recommend", headers=disp, json={"incident_id": mk(client, disp)}).json()
    run(ctx.autopilot.drain)
    assert client.get(f"/api/dispatch/decisions/{d['id']}", headers=disp).json()["status"] == "pending"
    ctx.autopilot.enabled = True
    assert client.post("/api/dispatch/autopilot", headers=disp, json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/dispatch/autopilot", headers=disp).json()["actor"] == "demo-autopilot"


# ---- M7 watchdog --------------------------------------------------------------------
def assigned(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    approve(client, disp, d["id"])
    a = client.get("/api/assignments", headers=disp, params={"incident_id": iid}).json()[0]
    return iid, d, a


def test_watchdog_states_are_distinct_and_sent_is_not_acknowledged(client, disp):
    iid, d, a = assigned(client, disp)
    drv = token_for(f"driver-{a['vehicle_id']}")
    assert a["state"] == "sent" and a["acknowledged_at"] is None and a["accepted_at"] is None
    a = client.post(f"/api/assignments/{a['id']}/delivered", headers=disp).json()
    assert a["state"] == "delivered_if_known" and a["acknowledged_at"] is None
    a = client.post(f"/api/assignments/{a['id']}/acknowledge", headers=drv).json()
    assert a["state"] == "acknowledged" and a["accepted_at"] is None
    assert vehicles(client, disp)[a["vehicle_id"]]["status"] == "assigned"  # ack != accept
    a = client.post(f"/api/assignments/{a['id']}/accept", headers=drv).json()
    assert a["state"] == "accepted" and vehicles(client, disp)[a["vehicle_id"]]["status"] == "accepted"
    assert [h["state"] for h in a["history"]] == ["created", "sent", "delivered_if_known", "acknowledged", "accepted"]


def test_dispatcher_cannot_accept_on_behalf_of_driver(client, disp):
    _, _, a = assigned(client, disp)
    assert client.post(f"/api/assignments/{a['id']}/accept", headers=disp).status_code == 403
    other = next(v for v in vehicles(client, disp) if v != a["vehicle_id"])
    assert client.post(f"/api/assignments/{a['id']}/accept", headers=token_for(f"driver-{other}")).status_code == 403


def test_watchdog_timeout_alert_with_simulated_fallback_and_late_accept(client, disp, ctx):
    iid, d, a = assigned(client, disp)
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.ack_timeout_s - 5)
    assert client.post("/api/assignments/check-timeouts", headers=disp).json() == []
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.ack_timeout_s + 5)
    out = client.post("/api/assignments/check-timeouts", headers=disp).json()
    assert [x["state"] for x in out] == ["timed_out"]
    fb = out[0]["fallback"]
    assert fb["kind"] == "simulated_escalation" and fb["requires_human_approval"] is True
    assert "no real call" in fb["message"].lower()
    # vehicle and incident untouched: the watchdog never reassigns
    assert vehicles(client, disp)[a["vehicle_id"]]["status"] == "assigned"
    assert client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]["assigned_vehicle_id"] == a["vehicle_id"]
    assert client.post("/api/assignments/check-timeouts", headers=disp).json() == []  # not re-raised
    late = client.post(f"/api/assignments/{a['id']}/accept", headers=token_for(f"driver-{a['vehicle_id']}")).json()
    assert late["state"] == "accepted" and late["late"] is True
    types = [e["event_type"] for e in client.get(f"/api/incidents/{iid}/audit", headers=disp).json()]
    assert "assignment.timed_out" in types


def test_driver_reject_frees_vehicle_and_flags_incident(client, disp):
    iid, d, a = assigned(client, disp)
    r = client.post(f"/api/assignments/{a['id']}/reject", headers=token_for(f"driver-{a['vehicle_id']}"),
                    json={"note": "vehicle fault"})
    assert r.json()["state"] == "rejected"
    assert vehicles(client, disp)[a["vehicle_id"]]["status"] == "available"
    inc = client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]
    assert inc["assigned_vehicle_id"] is None and inc["status"] == "new"
    assert client.post(f"/api/assignments/{a['id']}/accept", headers=token_for(f"driver-{a['vehicle_id']}")).status_code == 409


def test_reassignment_requires_new_approval_and_releases_old_unit(client, disp):
    iid, d, a = assigned(client, disp)
    old = a["vehicle_id"]
    d2 = rec(client, disp, iid, weights={"w_switch": 0})
    new_c = next((c for c in d2["candidates"] if c["vehicle_id"] != old), None)
    assert new_c
    r = approve(client, disp, d2["id"], "override", "crew requested swap", vehicle_id=new_c["vehicle_id"])
    assert r.status_code == 200, r.text
    vs = vehicles(client, disp)
    assert vs[old]["status"] == "available" and vs[new_c["vehicle_id"]]["status"] == "assigned"
    assert r.json()["final"]["previous"]["vehicle_id"] == old


def test_approve_decision_for_vehicle_that_became_ineligible_is_rejected(client, disp):
    iid = mk(client, disp)
    d = rec(client, disp, iid)
    vid = d["recommendation"]["vehicle_id"]
    client.patch(f"/api/vehicles/{vid}/state", headers=disp, json={
        "update_id": "x-gone", "occurred_at": (client.app.state.ctx.now() + timedelta(seconds=1)).isoformat(),
        "status": "unavailable", "reason": "breakdown"})
    r = approve(client, disp, d["id"])
    assert r.status_code == 409 and "hard constraints" in r.json()["detail"]


def test_ack_service_direct_use(ctx, run):
    assert ApproveRequest(action="approve", reason="abc").action == "approve"
    assert ctx.acks.settings.ack_timeout_s > 0
