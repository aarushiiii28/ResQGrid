import csv
import io
import json

import pytest

from app.schemas.common import Source
from tests.conftest import token_for


def create(client, who, **body):
    r = client.post("/api/incidents", headers=who, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_incident_provenance_and_unknowns(client, disp, caller):
    inc = create(client, disp, reported_category="cardiac", operator_priority=1,
                 location={"lat": 30.9, "lon": 75.85, "accuracy_m": 12}, text="man collapsed at bus stand")
    assert inc["id"].startswith("INC-") and inc["demo_mode"] is True
    assert inc["reported_category"]["source"] == "human_confirmed"
    assert "patient_count" in inc["unknowns"] and "conscious" in inc["unknowns"]
    c = create(client, caller, text="help")
    assert c["reported_category"] is None and c["created_by"] == "caller-1"


def test_raw_reports_stored_separately(client, disp):
    inc = create(client, disp, text="raw words here", location={"lat": 30.9, "lon": 75.85})
    d = client.get(f"/api/incidents/{inc['id']}", headers=disp).json()
    assert [r["text"] for r in d["reports"]] == ["raw words here"]
    assert "raw words here" not in json.dumps(d["incident"])


def test_fact_precedence_and_conflict(client, disp):
    inc = create(client, disp)
    iid = inc["id"]

    def setf(**kw):
        r = client.post(f"/api/incidents/{iid}/facts", headers=disp, json=kw)
        assert r.status_code == 200, r.text
        return r.json()

    setf(key="conscious", value=False, source="reported", confidence=0.7)
    i = setf(key="conscious", value=True, source="reported", confidence=0.6)  # equal rank: kept + flagged
    assert i["facts"]["conscious"]["value"] is False
    assert any(c["field"] == "conscious" and not c["resolved"] for c in i["conflicts"])
    i = setf(key="conscious", value=True, source="human_confirmed")
    assert i["facts"]["conscious"]["value"] is True and i["facts"]["conscious"]["human_confirmed"]
    assert all(c["resolved"] for c in i["conflicts"] if c["field"] == "conscious")
    i = setf(key="conscious", value=False, source="reported")  # lower rank cannot override confirmed
    assert i["facts"]["conscious"]["value"] is True


def test_operators_cannot_submit_ai_facts_via_api(client, disp):
    iid = create(client, disp)["id"]
    r = client.post(f"/api/incidents/{iid}/facts", headers=disp,
                    json={"key": "k", "value": 1, "source": "ai_extracted"})
    assert r.status_code == 422


def test_ai_cannot_downgrade_urgency(client, disp, ctx, run):
    from app.services.incident_service import RuleViolation

    iid = create(client, disp, operator_priority=1)["id"]
    with pytest.raises(RuleViolation):
        run(ctx.incidents.set_fact, iid, "operator_priority", 3, source=Source.ai_extracted,
            confidence=0.9, actor="extractor")
    ok = run(ctx.incidents.set_fact, iid, "operator_priority", 1, source=Source.ai_extracted,
             confidence=0.9, actor="extractor")
    assert ok.operator_priority.value == 1


def test_duplicate_suggested_never_auto_merged(client, disp):
    a = create(client, disp, location={"lat": 30.9, "lon": 75.85, "accuracy_m": 10},
               text="man collapsed outside bus stand")
    b = create(client, disp, location={"lat": 30.9002, "lon": 75.8501, "accuracy_m": 10},
               text="man collapsed near the bus stand")
    assert [s["incident_id"] for s in b["duplicate_suggestions"]] == [a["id"]]
    for iid in (a["id"], b["id"]):
        assert client.get(f"/api/incidents/{iid}", headers=disp).json()["incident"]["status"] == "new"
    r = client.post(f"/api/incidents/{b['id']}/reports", headers=disp, json={"text": "second caller"})
    assert r.json()["auto_merged"] is False


def test_merge_unmerge_reversible(client, disp):
    a = create(client, disp, location={"lat": 30.9, "lon": 75.85}, text="report alpha")
    b = create(client, disp, location={"lat": 30.9, "lon": 75.85}, text="report beta")
    client.post(f"/api/incidents/{b['id']}/facts", headers=disp,
                json={"key": "bleeding", "value": True, "source": "reported"})
    m = client.post(f"/api/incidents/{b['id']}/merge", headers=disp,
                    json={"target_id": a["id"], "reason": "same scene"})
    assert m.status_code == 200 and b["id"] in m.json()["merged_from"]
    da = client.get(f"/api/incidents/{a['id']}", headers=disp).json()
    assert sorted(r["text"] for r in da["reports"]) == ["report alpha", "report beta"]
    assert "bleeding" in da["incident"]["facts"]
    db = client.get(f"/api/incidents/{b['id']}", headers=disp).json()["incident"]
    assert db["status"] == "merged" and db["merged_into"] == a["id"]

    u = client.post(f"/api/incidents/{b['id']}/unmerge", headers=disp, params={"reason": "wrong call"})
    assert u.status_code == 200 and u.json()["status"] == "new"
    da = client.get(f"/api/incidents/{a['id']}", headers=disp).json()
    assert [r["text"] for r in da["reports"]] == ["report alpha"]
    assert "bleeding" not in da["incident"]["facts"] and da["incident"]["merged_from"] == []
    types = [e["event_type"] for e in da["timeline"]]
    assert "incident.merged" in types


def test_split_creates_new_incident_with_reports(client, disp):
    a = create(client, disp, location={"lat": 30.9, "lon": 75.85}, text="first")
    r2 = client.post(f"/api/incidents/{a['id']}/reports", headers=disp, json={"text": "second"}).json()
    s = client.post(f"/api/incidents/{a['id']}/split", headers=disp,
                    json={"report_ids": [r2["report"]["id"]], "reason": "different patient"})
    assert s.status_code == 201 and s.json()["split_from"] == a["id"]
    d = client.get(f"/api/incidents/{s.json()['id']}", headers=disp).json()
    assert [r["text"] for r in d["reports"]] == ["second"]


def test_audit_timeline_and_exports(client, disp, admin):
    iid = create(client, disp, reported_category="trauma", text="crash")["id"]
    client.post(f"/api/incidents/{iid}/facts", headers=disp,
                json={"key": "conscious", "value": True, "source": "human_confirmed", "reason": "caller said"})
    j = client.get(f"/api/incidents/{iid}/audit", headers=disp, params={"format": "json"})
    events = j.json()
    assert [e["event_type"] for e in events][:2] == ["incident.created", "incident.fact_set"]
    assert len({e["event_id"] for e in events}) == len(events)
    assert all(e["correlation_id"] == iid for e in events)
    assert events[1]["reason"] == "caller said" and events[1]["actor"] == "disp-1"
    c = client.get(f"/api/incidents/{iid}/audit", headers=disp, params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(c.text)))
    assert [r["event_id"] for r in rows] == [e["event_id"] for e in events]
    assert "text/csv" in c.headers["content-type"]


def test_audit_is_append_only(client, run, ctx):
    iid = create(client, token_for(role="dispatcher"))["id"]

    async def tamper():
        async with ctx.store.pool.connection() as conn:
            await conn.execute("update audit_events set actor='x' where correlation_id=%s", (iid,))

    with pytest.raises(Exception, match="append-only"):
        run(tamper)

    async def remove():
        async with ctx.store.pool.connection() as conn:
            await conn.execute("delete from audit_events where correlation_id=%s", (iid,))

    with pytest.raises(Exception, match="append-only"):
        run(remove)


# ---- authorization ----------------------------------------------------
def test_requires_token(client):
    assert client.get("/api/incidents").status_code == 401
    assert client.get("/api/vehicles", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_route_level_permissions(client, caller):
    assert client.get("/api/vehicles", headers=caller).status_code == 403
    assert client.get("/api/hospitals", headers=caller).status_code == 403
    assert client.get("/api/events", headers=caller).status_code == 403
    assert client.post("/api/admin/reset-demo", headers=token_for(role="dispatcher")).status_code == 403
    assert client.patch("/api/hospitals/HOSP-01/resources", headers=token_for(role="dispatcher"),
                        json={}).status_code == 403


def test_caller_sees_only_own_incidents(client, disp):
    mine = create(client, token_for("caller-1"), text="mine")
    theirs = create(client, token_for("caller-2"), text="theirs")
    ids = [i["id"] for i in client.get("/api/incidents", headers=token_for("caller-1")).json()]
    assert ids == [mine["id"]]
    assert client.get(f"/api/incidents/{theirs['id']}", headers=token_for("caller-1")).status_code == 403


def test_hospital_sees_only_assigned_cases(client, disp, ctx, run):
    a = create(client, disp, text="a")
    b = create(client, disp, text="b")
    run(ctx.incidents.assign, a["id"], "AMB-01", "HOSP-01", "disp-1", "test")
    h1, h2 = token_for("hosp-HOSP-01"), token_for("hosp-HOSP-02")
    assert [i["id"] for i in client.get("/api/incidents", headers=h1).json()] == [a["id"]]
    assert client.get("/api/incidents", headers=h2).json() == []
    assert client.get(f"/api/incidents/{a['id']}", headers=h2).status_code == 403
    assert client.get(f"/api/incidents/{b['id']}", headers=h1).status_code == 403
    d = client.get(f"/api/incidents/{a['id']}", headers=h1).json()
    assert "timeline" not in d  # hospitals do not read the full audit log


def test_driver_scoped_to_own_vehicle(client):
    d = token_for("driver-AMB-01")
    assert [v["id"] for v in client.get("/api/vehicles", headers=d).json()] == ["AMB-01"]
    r = client.patch("/api/vehicles/AMB-02/state", headers=d,
                     json={"update_id": "d1", "occurred_at": "2026-10-22T00:00:00Z", "status": "unavailable"})
    assert r.status_code == 403
    r = client.patch("/api/vehicles/AMB-01/state", headers=d,
                     json={"update_id": "d2", "occurred_at": "2026-10-22T00:00:00Z", "status": "assigned",
                           "incident_id": "INC-1"})
    assert r.status_code == 403  # drivers cannot self-assign


def test_hospital_cannot_update_other_hospital(client):
    r = client.patch("/api/hospitals/HOSP-02/resources", headers=token_for("hosp-HOSP-01"),
                     json={"update_id": "q", "occurred_at": "2026-10-22T00:00:00Z",
                           "resource": "er_bay", "status": "available"})
    assert r.status_code == 403


def test_demo_login_issues_working_token(client):
    r = client.post("/api/auth/demo-login", json={"role": "dispatcher"})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert client.get("/api/incidents", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.post("/api/auth/demo-login", json={"user_id": "nobody"}).status_code == 404
    assert len(client.get("/api/auth/users").json()) >= 5
