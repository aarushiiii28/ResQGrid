from datetime import UTC, datetime

from tests.conftest import token_for

ENVELOPE = {"event_id", "event_type", "occurred_at", "entity_id", "correlation_id", "source",
            "payload", "seq"}


def tok(**kw):
    return token_for(**kw)["Authorization"].split()[1]


def test_ws_rejects_bad_token(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events?token=bad"):
            pass


def test_ws_receives_envelope_events(client):
    disp = token_for(role="dispatcher")
    with client.websocket_connect(f"/ws/events?token={tok(role='dispatcher')}") as ws:
        hello = ws.receive_json()
        assert hello["event_type"] == "sync.hello"
        r = client.post("/api/incidents", headers=disp, json={"text": "ws test"})
        ev = ws.receive_json()
        assert ENVELOPE <= set(ev)
        assert ev["event_type"] == "incident.created" and ev["correlation_id"] == r.json()["id"]
        assert ev["seq"] > hello["seq"]


def test_ws_reconnect_resyncs_missed_events(client):
    disp = token_for(role="dispatcher")
    with client.websocket_connect(f"/ws/events?token={tok(role='dispatcher')}") as ws:
        last = ws.receive_json()["seq"]
    # events happen while disconnected
    ids = [client.post("/api/incidents", headers=disp, json={"text": f"t{i}"}).json()["id"] for i in range(2)]
    with client.websocket_connect(f"/ws/events?token={tok(role='dispatcher')}&since={last}") as ws:
        ws.receive_json()  # hello
        got = [ws.receive_json()["correlation_id"] for _ in range(2)]
    assert got == ids


def test_ws_resync_required_when_gap_too_large(client, ctx):
    ctx.hub.buffer.clear()
    ctx.hub.seq = 500
    ctx.hub.publish("x.y", "E", vis=None)
    with client.websocket_connect(f"/ws/events?token={tok(role='dispatcher')}&since=0") as ws:
        ws.receive_json()
        assert ws.receive_json()["event_type"] == "sync.resync_required"


def test_ws_events_filtered_by_role(client, ctx, run):
    disp = token_for(role="dispatcher")
    a = client.post("/api/incidents", headers=disp, json={"text": "a"}).json()["id"]
    run(ctx.incidents.assign, a, "AMB-01", "HOSP-01", "disp-1", "t")
    with client.websocket_connect(f"/ws/events?token={tok(user_id='hosp-HOSP-02')}&since=0") as ws2:
        ws2.receive_json()
        client.patch("/api/hospitals/HOSP-02/resources", headers=token_for("hosp-HOSP-02"),
                     json={"update_id": "w1", "occurred_at": datetime.now(UTC).isoformat(),
                           "resource": "er_bay", "status": "available", "count": 2})
        ev = ws2.receive_json()
        assert ev["entity_id"] == "HOSP-02"  # saw own hospital, not incident INC events
    with client.websocket_connect(f"/ws/events?token={tok(user_id='hosp-HOSP-01')}&since=0") as ws1:
        ws1.receive_json()
        # only the assigned case is visible to HOSP-01 (not incident.created, which has no hospital)
        assert ws1.receive_json()["event_type"] == "incident.assigned"
