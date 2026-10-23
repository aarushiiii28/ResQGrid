import copy
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import token_for

HERE = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 12.0, "location_age_s": 1.0}
CALLER = lambda: token_for("caller-1")  # noqa: E731
DISP = lambda: token_for(role="dispatcher")  # noqa: E731
ADMIN = lambda: token_for(role="admin")  # noqa: E731


@pytest.fixture(autouse=True)
def _local_noon(client):
    """The model has a small night-time term; pin the local clock to noon so these tests do not depend on when they run."""
    client.app.state.ctx.false_alarm.base_cfg["tz_offset_h"] = (12 - datetime.now(UTC).hour) % 24


def fa(c):
    return c.app.state.ctx.false_alarm


def tick(c):
    c.portal.call(fa(c).tick)


def drain(c):
    c.portal.call(c.app.state.ctx.drain)


def start(c, **kw):
    r = c.post("/api/sos/start", headers=CALLER(), json={**HERE, **kw})
    assert r.status_code == 201, r.text
    return r.json()["incident_id"]


def confirm(c, iid):
    r = c.post(f"/api/incidents/{iid}/sos/confirm", headers=CALLER())
    assert r.status_code == 200, r.text
    return r.json()


def view(c, iid):
    r = c.get(f"/api/incidents/{iid}/false-alarm", headers=DISP())
    assert r.status_code == 200, r.text
    return r.json()


def incident(c, iid):
    return c.get(f"/api/incidents/{iid}", headers=DISP()).json()["incident"]


def decisions(c, iid):
    return c.get("/api/dispatch/decisions", headers=DISP(), params={"incident_id": iid}).json()


def signals(c, iid, feats, mic=False):
    body = {"mic_consent": mic, "features": [{"name": n, **v} for n, v in feats.items()]}
    r = c.post(f"/api/incidents/{iid}/signals", headers=CALLER(), json=body)
    assert r.status_code == 200, r.text
    return r.json()


class FakeClock:
    """Deterministic time for the ladder and hold: no sleeping, no flakiness under load."""

    def __init__(self):
        self.t = datetime.now(UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t = self.t + timedelta(seconds=s)


@pytest.fixture
def clock(client):
    c = FakeClock()
    client.app.state.ctx.clock = c
    return c


def audit_types(c, iid):
    return [e["event_type"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP()).json()]


# ---- layer 1 -------------------------------------------------------------------------------------
def test_pending_incident_is_visible_but_not_dispatched_and_live_after_confirm(client):
    iid = start(client)
    inc = incident(client, iid)
    assert inc["status"] == "pending" and inc["assigned_vehicle_id"] is None
    listed = {i["id"]: i for i in client.get("/api/incidents", headers=DISP()).json()}
    assert listed[iid]["status"] == "pending"
    assert client.post("/api/dispatch/recommend", headers=DISP(), json={"incident_id": iid}).status_code in (409, 422)
    assert decisions(client, iid) == []
    r = confirm(client, iid)
    assert r["status"] == "new" and r["call_id"].startswith("CALL-")
    assert confirm(client, iid)["call_id"] == r["call_id"]          # idempotent
    assert incident(client, iid)["status"] == "new"
    drain(client)
    assert decisions(client, iid)                                   # UNCERTAIN lane dispatches straight away
    types = audit_types(client, iid)
    assert types.index("incident.pending_created") < types.index("incident.went_live")


def test_cancel_keeps_the_record_forever_and_queues_one_check_in(client):
    iid = start(client)
    r = client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER())
    assert r.status_code == 200 and r.json()["status"] == "cancelled_by_caller"
    inc = incident(client, iid)
    assert inc["status"] == "cancelled_by_caller" and inc["assigned_vehicle_id"] is None
    assert iid in {i["id"] for i in client.get("/api/incidents", headers=DISP()).json()}   # nothing deleted
    assert client.get(f"/api/incidents/{iid}/check-in", headers=CALLER()).json()["checkin"]["state"] == "prompting"
    assert client.post(f"/api/incidents/{iid}/sos/confirm", headers=CALLER()).status_code == 409
    drain(client)
    assert decisions(client, iid) == []
    types = audit_types(client, iid)
    assert "incident.cancelled_by_caller" in types and "false_alarm.checkin_prompt" in types
    assert client.delete(f"/api/incidents/{iid}", headers=DISP()).status_code in (404, 405)


def test_a_cancelled_caller_is_always_asked_even_if_the_context_looks_high(client):
    iid = start(client)
    signals(client, iid, {"crash_like": {"value": 0.9, "status": "available"}})        # looks like a crash...
    assert view(client, iid)["lane"] == "HIGH"
    client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER())                   # ...but the caller cancelled
    ci = client.get(f"/api/incidents/{iid}/check-in", headers=CALLER()).json()
    assert ci["prompt"] and ci["checkin"]["prompt_n"] == 1


def test_cancel_after_the_countdown_is_refused(client):
    iid = start(client)
    confirm(client, iid)
    assert client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER()).status_code == 409


def test_app_that_vanishes_mid_countdown_is_failed_safe_to_live(client, clock):
    iid = start(client)
    tick(client)
    assert incident(client, iid)["status"] == "pending"                  # still inside the countdown + grace
    clock.advance(fa(client).base_cfg["countdown_s"] + fa(client).base_cfg["pending_grace_s"] + 1)
    tick(client)
    assert incident(client, iid)["status"] == "new"
    v = view(client, iid)
    assert v["lane"] in ("UNCERTAIN", "HIGH")
    assert "countdown_elapsed_no_client" in " ".join(
        str(e.get("reason")) for e in client.get(f"/api/incidents/{iid}/audit", headers=DISP()).json())


def test_other_callers_cannot_touch_a_pending_incident(client):
    iid = start(client)
    other = token_for("caller-2")
    assert client.post(f"/api/incidents/{iid}/sos/cancel", headers=other).status_code == 403
    assert client.post(f"/api/incidents/{iid}/sos/confirm", headers=other).status_code == 403
    assert client.post(f"/api/incidents/{iid}/signals", headers=other, json={"features": []}).status_code == 403


# ---- layer 2 / 5 ---------------------------------------------------------------------------------
def test_crash_like_signal_forces_high(client):
    iid = start(client)
    confirm(client, iid)
    out = signals(client, iid, {"crash_like": {"value": 0.9, "status": "available"},
                                "touch_random": {"value": 1.0, "status": "available"},
                                "pocket_motion": {"value": 1.0, "status": "available"}})
    assert "lane" not in out                                        # the caller never sees the lane
    v = view(client, iid)
    assert v["lane"] == "HIGH" and "crash_like" in v["overrides"]
    assert v["model_version"] and v["config_hash"] and v["contributions"]
    assert abs(v["bias"] + sum(c["contribution"] for c in v["contributions"]) - v["logit"]) < 0.01


def test_unavailable_sensors_do_not_lower_the_score(client):
    a = start(client)
    confirm(client, a)
    p0 = view(client, a)["probability_genuine"]
    signals(client, a, {n: {"value": None, "status": "unavailable"} for n in ("crash_like", "motion_erratic", "pocket_motion")})
    v = view(client, a)
    assert v["probability_genuine"] == p0 and "crash_like" in v["missing"]


def test_mic_numbers_are_ignored_and_dropped_without_consent(client):
    iid = start(client)
    confirm(client, iid)
    signals(client, iid, {"scream": {"value": 0.95, "status": "available"}}, mic=False)
    v = view(client, iid)
    assert "scream" in v["missing"] and v["mic_consent"] is False
    signals(client, iid, {"scream": {"value": 0.95, "status": "available"}}, mic=True)
    assert view(client, iid)["probability_genuine"] > v["probability_genuine"]
    signals(client, iid, {}, mic=False)                              # consent withdrawn: numbers forgotten
    assert view(client, iid)["probability_genuine"] == v["probability_genuine"]


def test_unknown_signal_names_and_extra_fields_are_rejected(client):
    iid = start(client)
    confirm(client, iid)
    assert client.post(f"/api/incidents/{iid}/signals", headers=CALLER(),
                       json={"features": [{"name": "audio_blob", "value": 1}]}).status_code == 409
    assert client.post(f"/api/incidents/{iid}/signals", headers=CALLER(),
                       json={"features": [{"name": "scream", "value": 0.2, "pcm": "AAAA"}]}).status_code == 422
    assert client.post(f"/api/incidents/{iid}/signals", headers=CALLER(), json={"features": [], "audio": "AAAA"}).status_code == 422


def test_no_raw_audio_path_exists_in_code(client):
    root = Path(__file__).resolve().parents[2] / "app"
    src = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    assert not re.search(r"UploadFile|multipart|octet-stream|MediaRecorder|\.wav|\.webm|\.mp3", src)
    spec = client.get("/openapi.json").json()
    for path, ops in spec["paths"].items():
        for op in ops.values():
            for ct in (op.get("requestBody", {}).get("content", {}) or {}):
                assert ct == "application/json", (path, ct)
    web = Path(__file__).resolve().parents[3] / "frontend" / "src"
    fe = "\n".join(p.read_text(encoding="utf-8") for p in web.rglob("*.ts*") if ".test." not in p.name)
    assert "MediaRecorder" not in fe and "recordedChunks" not in fe


# ---- layer 3 -------------------------------------------------------------------------------------
def test_corroboration_by_other_nearby_callers_raises_the_score(client):
    a = start(client)
    confirm(client, a)
    p0 = view(client, a)["probability_genuine"]
    r = client.post("/api/sos", headers=token_for("caller-2"), json=HERE)
    assert r.status_code == 201
    signals(client, a, {})
    v = view(client, a)
    assert v["corroborating"] and v["probability_genuine"] > p0
    assert any(c["feature"] == "corroboration" and c["contribution"] > 0 for c in v["contributions"])


def test_device_history_of_accidental_presses_lowers_the_score(client):
    ids = []
    for _ in range(3):
        iid = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-kid-1"}).json()["incident_id"]
        client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER())
        ids.append(iid)
    j = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-kid-1"}).json()["incident_id"]
    confirm(client, j)
    v = view(client, j)
    hist = [c for c in v["contributions"] if c["feature"] == "device_history"][0]
    assert hist["contribution"] < 0 and "3 earlier" in hist["note"]
    assert "dev-kid-1" not in str(client.get(f"/api/incidents/{j}/audit", headers=DISP()).json())   # only the hash is kept


# ---- layer 4 -------------------------------------------------------------------------------------
def test_check_in_ladder_escalates_and_never_dismisses(client, clock):
    iid = start(client)
    confirm(client, iid)
    tick(client)
    ci = client.get(f"/api/incidents/{iid}/check-in", headers=CALLER()).json()
    assert ci["prompt"] and ci["checkin"]["prompt_n"] == 1 and ci["checkin"]["volume"] == 0.5
    clock.advance(31)
    tick(client)
    ci = client.get(f"/api/incidents/{iid}/check-in", headers=CALLER()).json()["checkin"]
    assert ci["prompt_n"] == 3 and ci["volume"] == 1.0           # repeated, louder
    clock.advance(15)
    tick(client)
    v = view(client, iid)
    assert v["checkin"]["state"] == "unanswered" and v["lane"] in ("UNCERTAIN", "HIGH")
    assert incident(client, iid)["status"] in ("new", "dispatched", "active")      # escalated, not closed
    alerts = client.get("/api/alerts", headers=DISP()).json()
    assert any(a["kind"] == "checkin_unanswered" for a in alerts)


def test_i_am_safe_lowers_the_score_but_leaves_the_incident_open_until_a_dispatcher_closes_it(client):
    iid = start(client)
    confirm(client, iid)
    signals(client, iid, {"touch_random": {"value": 0.9, "status": "available"}, "pocket_motion": {"value": 0.9, "status": "available"}})
    before = view(client, iid)
    r = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "safe"})
    assert r.status_code == 200
    after = view(client, iid)
    assert after["probability_genuine"] < before["probability_genuine"]
    assert incident(client, iid)["status"] != "closed"
    short = client.post(f"/api/incidents/{iid}/close", headers=DISP(), json={"reason": "x"})
    assert short.status_code == 422                                  # a reason is required
    ok = client.post(f"/api/incidents/{iid}/close", headers=DISP(), json={"reason": "caller confirmed safe by phone"})
    assert ok.status_code == 200 and ok.json()["status"] == "closed"
    assert incident(client, iid)["status"] == "closed"
    assert "incident.closed_by_dispatcher" in audit_types(client, iid)


def test_help_words_force_high_and_negations_do_not(client):
    iid = start(client)
    r = confirm(client, iid)
    signals(client, iid, {"touch_random": {"value": 1.0, "status": "available"}, "pocket_motion": {"value": 1.0, "status": "available"}})
    u = client.post(f"/api/calls/{r['call_id']}/utterances", headers=CALLER(), json={"text": "there was an accident, please help", "mode": "typed"})
    assert u.status_code == 200
    v = view(client, iid)
    assert v["lane"] == "HIGH" and "explicit_help" in v["overrides"]
    j = start(client)
    r2 = confirm(client, j)
    fast = client.post(f"/api/calls/{r2['call_id']}/utterances", headers=CALLER(), json={"text": "sorry, pressed by mistake, i am fine", "mode": "typed"})
    assert fast.status_code == 200 and view(client, j)["checkin"]["state"] == "safe"


def test_cancelled_then_help_reopens_the_request(client):
    iid = start(client)
    client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER())
    r = client.post(f"/api/incidents/{iid}/check-in", headers=CALLER(), json={"response": "help"})
    assert r.status_code == 200
    assert incident(client, iid)["status"] == "new"
    drain(client)
    assert decisions(client, iid)
    assert "incident.reopened_by_checkin" in audit_types(client, iid)


def test_cancelled_and_silent_raises_a_followup_alert_not_a_dismissal(client, clock):
    iid = start(client)
    client.post(f"/api/incidents/{iid}/sos/cancel", headers=CALLER())
    tick(client)
    clock.advance(46)
    tick(client)
    alerts = client.get("/api/alerts", headers=DISP()).json()
    assert any(a["kind"] == "checkin_unanswered" and a["incident_id"] == iid for a in alerts)
    assert incident(client, iid)["status"] == "cancelled_by_caller"


# ---- lanes / hold ----------------------------------------------------------------------------------
def _make_low(c):
    iid = start(c)
    confirm(c, iid)
    signals(c, iid, {"touch_random": {"value": 1.0, "status": "available"}, "pocket_motion": {"value": 1.0, "status": "available"},
                     "device_idle": {"value": 1.0, "status": "available"}})
    return iid


def test_low_lane_waits_for_verification_then_dispatches_never_longer(client, clock):
    # make the picture clearly accidental: repeated accidental presses from the same device
    for _ in range(3):
        j = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-low"}).json()["incident_id"]
        client.post(f"/api/incidents/{j}/sos/cancel", headers=CALLER())
    iid = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-low"}).json()["incident_id"]
    confirm(client, iid)
    signals(client, iid, {"touch_random": {"value": 1.0, "status": "available"}, "pocket_motion": {"value": 1.0, "status": "available"},
                          "device_idle": {"value": 1.0, "status": "available"}, "mic_silence": {"value": 0.9, "status": "available"}}, mic=True)
    v = view(client, iid)
    assert v["lane"] == "LOW" and v["hold_remaining_s"] is not None and not v["dispatch_started"]
    drain(client)
    assert decisions(client, iid) == []                               # waiting, bounded
    clock.advance(fa(client).base_cfg["low_hold_s"] + 1)
    tick(client)
    drain(client)
    v = view(client, iid)
    assert v["dispatch_started"] and v["lane"] in ("UNCERTAIN", "HIGH")   # floor: never dismissed
    assert decisions(client, iid)


def test_dispatcher_can_dispatch_a_low_lane_incident_immediately(client):
    for _ in range(3):
        j = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-low2"}).json()["incident_id"]
        client.post(f"/api/incidents/{j}/sos/cancel", headers=CALLER())
    iid = client.post("/api/sos/start", headers=CALLER(), json={**HERE, "device_id": "dev-low2"}).json()["incident_id"]
    confirm(client, iid)
    signals(client, iid, {"touch_random": {"value": 1.0, "status": "available"}, "pocket_motion": {"value": 1.0, "status": "available"}})
    assert view(client, iid)["lane"] == "LOW"
    assert client.post(f"/api/incidents/{iid}/false-alarm/dispatch", headers=DISP()).status_code == 200
    drain(client)
    assert decisions(client, iid)
    assert "false_alarm.dispatch_started" in audit_types(client, iid)


def test_tag_outcome_requires_reason_and_feeds_device_history(client):
    iid = start(client, device_id="dev-tag")
    confirm(client, iid)
    assert client.post(f"/api/incidents/{iid}/false-alarm/tag", headers=DISP(), json={"outcome": "accidental", "reason": "x"}).status_code == 422
    r = client.post(f"/api/incidents/{iid}/false-alarm/tag", headers=DISP(), json={"outcome": "accidental", "reason": "child pressed it"})
    assert r.status_code == 200 and r.json()["tag"]["outcome"] == "accidental"
    j = start(client, device_id="dev-tag")
    confirm(client, j)
    assert any(c["feature"] == "device_history" and c["contribution"] < 0 for c in view(client, j)["contributions"])


def test_every_decision_is_persisted_with_version_hash_and_trace(client):
    iid = start(client)
    confirm(client, iid)
    signals(client, iid, {"crash_like": {"value": 0.8, "status": "available"}})
    rec = client.portal.call(fa(client).get, iid)
    assert len(rec.assessments) >= 2
    for a in rec.assessments:
        assert a.model_version == "fa-v1-heuristic" and len(a.config_hash) == 16 and a.contributions
    events = [e for e in client.get(f"/api/incidents/{iid}/audit", headers=DISP()).json() if e["event_type"] == "false_alarm.assessed"]
    assert events and events[-1]["payload"]["model_version"] and events[-1]["payload"]["config_hash"]


# ---- recalibration needs approval ------------------------------------------------------------------
def test_a_recalibrated_model_is_inactive_until_an_admin_approves_it(client):
    cur = client.get("/api/false-alarm/model", headers=DISP()).json()
    w = copy.deepcopy(cur["weights"])
    w["touch_random"] = 2.4
    r = client.post("/api/false-alarm/models", headers=ADMIN(), json={"version": "fa-v2-test", "weights": w, "bias": 0.4, "note": "test"})
    assert r.status_code == 201
    assert client.post("/api/false-alarm/models", headers=DISP(), json={"version": "fa-v3", "weights": w, "bias": 0.4}).status_code == 403
    iid = start(client)
    confirm(client, iid)
    assert view(client, iid)["model_version"] == "fa-v1-heuristic"            # no silent change
    assert client.post("/api/false-alarm/models/fa-v2-test/activate", headers=DISP(), json={"reason": "ok go"}).status_code == 403
    assert client.post("/api/false-alarm/models/fa-v2-test/activate", headers=ADMIN(), json={"reason": "validated on synthetic mix"}).status_code == 200
    j = start(client)
    confirm(client, j)
    v = view(client, j)
    assert v["model_version"] == "fa-v2-test"
    events = client.get("/api/incidents/" + j + "/audit", headers=DISP()).json()
    assert events
    again = client.post("/api/false-alarm/models", headers=ADMIN(), json={"version": "fa-v2-test", "weights": w, "bias": 0.4})
    assert again.status_code == 409


def test_pending_incident_cannot_be_driven_through_dispatch_or_hospital(client):
    iid = start(client)
    assert client.post("/api/dispatch/recommend", headers=DISP(), json={"incident_id": iid}).status_code in (409, 422)
    assert client.get("/api/prealerts", headers=token_for("hosp-HOSP-01")).json() == []
