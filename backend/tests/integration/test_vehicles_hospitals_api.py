from datetime import UTC, datetime, timedelta

from tests.conftest import token_for


def ts(offset_s=0):
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


def patch_v(client, vid, body, who=None):
    return client.patch(f"/api/vehicles/{vid}/state", json=body,
                        headers=who or token_for(role="dispatcher"))


def test_seed_counts(client, disp, admin):
    vs = client.get("/api/vehicles", headers=disp).json()
    hs = client.get("/api/hospitals", headers=disp).json()
    assert len(vs) == 20 and len(hs) == 12
    tags = {t for v in vs for t in v["capability_tags"]}
    assert {"BLS", "ALS", "cardiac", "neonatal", "trauma"} <= tags
    assert len(client.get("/api/aeds", headers=disp).json()) == 25
    assert len(client.get("/api/volunteers", headers=disp).json()) == 15
    assert len(client.get("/api/buildings", headers=disp).json()) == 10
    assert all(len(b["entrances"]) >= 2 for b in client.get("/api/buildings", headers=disp).json())


def test_valid_transition_and_audit(client, disp, admin):
    r = patch_v(client, "AMB-01", {"update_id": "u1", "occurred_at": ts(), "status": "assigned",
                                   "incident_id": "INC-1001"})
    assert r.json() == {"applied": True, "reason": "applied"}
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-01"][0]
    assert v["status"] == "assigned" and v["assigned_incident_id"] == "INC-1001"
    ev = client.get("/api/events", headers=admin, params={"entity_id": "AMB-01"}).json()
    assert ev[-1]["event_type"] == "vehicle.status_changed"
    assert (ev[-1]["old_value"], ev[-1]["new_value"]) == ("available", "assigned")


def test_invalid_transition_rejected_and_unchanged(client, disp):
    r = patch_v(client, "AMB-02", {"update_id": "u2", "occurred_at": ts(), "status": "en_route"})
    assert r.status_code == 409
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-02"][0]
    assert v["status"] == "available"


def test_assigned_requires_incident(client):
    r = patch_v(client, "AMB-03", {"update_id": "u3", "occurred_at": ts(), "status": "assigned"})
    assert r.status_code == 422


def test_duplicate_update_is_idempotent(client, disp):
    body = {"update_id": "dup-1", "occurred_at": ts(), "status": "unavailable"}
    assert patch_v(client, "AMB-04", body).json()["applied"] is True
    again = patch_v(client, "AMB-04", body).json()
    assert again == {"applied": False, "reason": "duplicate"}
    h = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-04"][0]
    assert sum(1 for e in h["history"] if e["kind"] == "status") == 1


def test_out_of_order_location_ignored(client, disp):
    new = {"update_id": "l2", "occurred_at": ts(10), "location": {"lat": 30.91, "lon": 75.86}}
    old = {"update_id": "l1", "occurred_at": ts(2), "location": {"lat": 30.95, "lon": 75.90}}
    assert patch_v(client, "AMB-05", new).json()["applied"] is True
    assert patch_v(client, "AMB-05", old).json() == {"applied": False, "reason": "out_of_order"}
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-05"][0]
    assert v["location"]["lat"] == 30.91


def test_future_timestamp_rejected(client):
    r = patch_v(client, "AMB-06", {"update_id": "f1", "occurred_at": ts(3600), "status": "unavailable"})
    assert r.status_code == 422


def test_stale_vehicle_becomes_unknown_via_clock(client, ctx, disp):
    base = ctx.now()
    ctx.clock = lambda: base + timedelta(seconds=ctx.settings.vehicle_stale_s + 30)
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-07"][0]
    assert v["availability"] == "unknown" and v["location_state"] == "stale"
    assert v["location_age_s"] > ctx.settings.vehicle_stale_s


def test_offline_then_reconnect(client, disp):
    patch_v(client, "AMB-08", {"update_id": "o1", "occurred_at": ts(), "offline": True})
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-08"][0]
    assert v["offline"] and v["availability"] == "unknown"
    patch_v(client, "AMB-08", {"update_id": "o2", "occurred_at": ts(), "offline": False})
    v = [x for x in client.get("/api/vehicles", headers=disp).json() if x["id"] == "AMB-08"][0]
    assert not v["offline"] and v["availability"] == "available"


def hosp(client, who, hid):
    return [h for h in client.get("/api/hospitals", headers=who).json() if h["id"] == hid][0]


def test_seed_has_stale_and_never_confirmed_hospitals(client, disp):
    h5, h10 = hosp(client, disp, "HOSP-05"), hosp(client, disp, "HOSP-10")
    assert h5["resources"]["cath_lab"]["stale"] and h5["resources"]["cath_lab"]["status"] != "available"
    assert h10["resources"]["er_bay"]["status"] == "unknown"


def test_hospital_confirm_deny_unknown_audited(client):
    who = token_for("hosp-HOSP-01")
    for i, st in enumerate(["unavailable", "available", "unknown"]):
        r = client.patch("/api/hospitals/HOSP-01/resources", headers=who,
                         json={"update_id": f"h{i}", "occurred_at": ts(i - 10), "resource": "icu_bed",
                               "status": st, "count": 1 if st == "available" else None,
                               "reason": "ward round"})
        assert r.json()["applied"] is True
    h = hosp(client, who, "HOSP-01")
    assert h["resources"]["icu_bed"]["status"] == "unknown"
    ev = client.get("/api/events", headers=token_for(role="admin"),
                    params={"entity_id": "HOSP-01"}).json()
    assert [e["new_value"]["status"] for e in ev] == ["unavailable", "available", "unknown"]
    assert ev[0]["reason"] == "ward round" and ev[0]["actor"] == "hosp-HOSP-01"


def test_hospital_update_idempotent_and_out_of_order(client):
    who = token_for("hosp-HOSP-02")
    body = {"update_id": "x1", "occurred_at": ts(-5), "resource": "er_bay", "status": "unavailable"}
    p = lambda b: client.patch("/api/hospitals/HOSP-02/resources", headers=who, json=b).json()  # noqa: E731
    assert p(body)["applied"] is True
    assert p(body) == {"applied": False, "reason": "duplicate"}
    older = {**body, "update_id": "x0", "occurred_at": ts(-500), "status": "available"}
    assert p(older) == {"applied": False, "reason": "out_of_order"}
    assert hosp(client, who, "HOSP-02")["resources"]["er_bay"]["status"] == "unavailable"


def test_unknown_resource_rejected(client):
    r = client.patch("/api/hospitals/HOSP-01/resources", headers=token_for("hosp-HOSP-01"),
                     json={"update_id": "z", "occurred_at": ts(), "resource": "helipad", "status": "available"})
    assert r.status_code == 422
