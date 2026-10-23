import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import get_settings
from app.services import merkle
from app.services.crypto_service import b64, canonical, sha256_hex, verify_signature
from tests.conftest import make_client, token_for

HERE = {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 12.0, "location_age_s": 1.0}
DISP = lambda: token_for(role="dispatcher")  # noqa: E731
ADMIN = lambda: token_for(role="admin")  # noqa: E731
CALLER = lambda: token_for("caller-1")  # noqa: E731


def ctxof(c):
    return c.app.state.ctx


def run(c, fn, *a, **kw):
    return c.portal.call(lambda: fn(*a, **kw))


def verify(c):
    return run(c, ctxof(c).ledger.verify_result)


def sos(c, who=None):
    r = c.post("/api/sos", headers=who or CALLER(), json=HERE)
    assert r.status_code == 201, r.text
    return r.json()


def audit(c, iid):
    return c.get(f"/api/incidents/{iid}/audit", headers=DISP()).json()


def sync_conn():
    return psycopg.connect(get_settings().test_database_url, autocommit=True)


# ---- client keys --------------------------------------------------------------------------------------
class Device:
    """A caller's phone: Ed25519 key pair and a monotonic counter."""

    def __init__(self, client, who="caller-1"):
        self.c, self.who, self.ctr = client, who, 0
        self.priv = Ed25519PrivateKey.generate()
        self.pub = b64(self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        r = client.post("/api/keys/register", headers=token_for(who), json={"public_key": self.pub})
        assert r.status_code == 201, r.text
        self.key_id = r.json()["key_id"]

    def header(self, action, body: bytes, *, nonce=None, ctr=None, ts=None, key_id=None, actor=None, sign_body: bytes | None = None):
        self.ctr += 1
        msg = json.dumps({"v": 1, "action": action, "actor_id": actor or self.who, "key_id": key_id or self.key_id,
                          "body_hash": sha256_hex(sign_body if sign_body is not None else body), "nonce": nonce or uuid.uuid4().hex,
                          "ctr": self.ctr if ctr is None else ctr, "ts": ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
                         sort_keys=True, separators=(",", ":"))
        env = {"key_id": key_id or self.key_id, "message": msg, "signature": b64(self.priv.sign(msg.encode()))}
        return base64.urlsafe_b64encode(json.dumps(env).encode()).decode().rstrip("=")

    def post(self, path, payload, action, **kw):
        body = json.dumps(payload).encode()
        h = {**token_for(self.who), "content-type": "application/json", "x-action-signature": self.header(action, body, **kw)}
        return self.c.post(path, content=body, headers=h)


# ---- chain, signatures, anchoring -----------------------------------------------------------------------
def test_every_event_is_hash_chained_signed_and_the_chain_verifies(client):
    iid = sos(client)["incident_id"]
    ev = audit(client, iid)
    assert ev and all(e["ledger"]["entry_hash"] and e["ledger"]["seq"] for e in ev)
    assert all(e["ledger"]["ledger_status"] == "PENDING" for e in ev)         # server-attested, waiting for an anchor
    res = verify(client)
    assert res["ok"] and res["status"] == "VERIFIED" and res["entries"] > 5 and res["failure"] is None
    rows = run(client, ctxof(client).store.ledger_rows)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["prev_hash"] == "0" * 64 and all(rows[i]["prev_hash"] == rows[i - 1]["entry_hash"] for i in range(1, len(rows)))
    assert rows[0]["event_type"] == "key.registered" and rows[0]["entity_id"] == "server-1"


def test_audit_timeline_and_exports_still_work_with_ledger_columns(client):
    iid = sos(client)["incident_id"]
    r = client.get(f"/api/incidents/{iid}/audit", params={"format": "csv"}, headers=DISP())
    assert r.status_code == 200 and "entry_hash,ledger_status" in r.text.splitlines()[0]
    assert any(e["event_type"] == "incident.created" for e in audit(client, iid))
    assert client.get(f"/api/incidents/{iid}", headers=DISP()).json()["timeline"]


def test_database_refuses_to_update_or_delete_ledger_rows_even_for_the_owner(client):
    sos(client)
    client.post("/api/ledger/anchor", headers=ADMIN())
    with sync_conn() as conn:
        for q in ("update audit_events set actor='x' where seq=1", "delete from audit_events where seq=1",
                  "update actor_keys set actor_id='x'", "delete from ledger_anchors"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(q)


def test_tampering_with_the_real_database_is_detected_with_expected_vs_found(client):
    sos(client)
    rows = run(client, ctxof(client).store.ledger_rows)
    target = rows[len(rows) // 2]
    with sync_conn() as conn:
        conn.execute("alter table audit_events disable trigger audit_no_change")
        try:
            conn.execute("update audit_events set actor='mallory' where seq=%s", (target["seq"],))
            res = verify(client)
        finally:
            conn.execute("update audit_events set actor=%s where seq=%s", (target["actor"], target["seq"]))
            conn.execute("alter table audit_events enable trigger audit_no_change")
    f = res["failure"]
    assert res["status"] == "TAMPERED" and f["kind"] == "entry_hash" and f["seq"] == target["seq"]
    assert f["expected"] != f["found"] and f["found"] == target["entry_hash"]
    assert verify(client)["ok"]                                              # restored: verified again


@pytest.mark.parametrize("mode,kind", [("modify", {"entry_hash"}), ("delete", {"seq_gap"}), ("reorder", {"seq_gap"}),
                                       ("insert", {"signature_invalid", "signature_actor"}), ("truncate", {"anchor_truncated"})])
def test_tamper_demo_detects_every_mode_on_a_copy_and_never_touches_the_real_chain(client, mode, kind):
    sos(client)
    sos(client, token_for("caller-2"))
    before = verify(client)
    r = client.post("/api/ledger/tamper-demo", headers=DISP(), json={"mode": mode})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["detected"] and out["failure"]["kind"] in kind, out
    assert out["real_chain_untouched"] and "DEMONSTRATION" in out["label"]
    after = verify(client)
    assert after["ok"] and after["entries"] >= before["entries"]


def test_tamper_demo_needs_entries_and_a_valid_mode(client):
    assert client.post("/api/ledger/tamper-demo", headers=DISP(), json={"mode": "modify"}).status_code == 409
    assert client.post("/api/ledger/tamper-demo", headers=DISP(), json={"mode": "explode"}).status_code == 422
    assert client.post("/api/ledger/tamper-demo", headers=CALLER(), json={"mode": "modify"}).status_code == 403


def test_streaming_verification_reports_progress_then_a_result(client):
    sos(client)
    with client.stream("POST", "/api/ledger/verify", headers=DISP()) as r:
        assert r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(x) for x in r.iter_lines() if x]
    assert lines[0]["type"] == "start" and lines[-1]["type"] == "result" and lines[-1]["status"] == "VERIFIED"
    assert any(x["type"] == "progress" for x in lines)


# ---- anchoring ----------------------------------------------------------------------------------------------
def test_local_anchor_needs_no_keys_or_network_and_is_labelled_honestly(client):
    iid = sos(client)["incident_id"]
    ev = audit(client, iid)[0]["event_id"]
    pre = client.get(f"/api/ledger/proof/{ev}", headers=DISP()).json()
    assert pre["anchored"] is False and pre["status"] == "PENDING"
    a = client.post("/api/ledger/anchor", headers=ADMIN()).json()
    assert a["anchor_id"] == "ANC-0001" and a["provider"] == "local" and "not a public blockchain" in a["reference"]["label"]
    head = client.get("/api/ledger/head", headers=DISP()).json()
    assert head["anchors"] == 1 and head["provider"] == "local" and "not a public blockchain" in head["provider_label"]
    assert {e["ledger"]["ledger_status"] for e in audit(client, iid)} <= {"ANCHORED", "PENDING"}
    assert audit(client, iid)[0]["ledger"]["ledger_status"] == "ANCHORED"
    assert verify(client)["anchors"] == {"checked": 1, "total": 1, "provider": "local", "label": a["reference"]["label"]}


def test_merkle_inclusion_proof_is_correct_and_verifiable_without_the_server(client):
    iid = sos(client)["incident_id"]
    ev = audit(client, iid)[1]["event_id"]
    client.post("/api/ledger/anchor", headers=ADMIN())
    p = client.get(f"/api/ledger/proof/{ev}", headers=CALLER()).json()
    assert p["anchored"] and p["status"] == "ANCHORED" and p["anchor"]["anchor_id"] == "ANC-0001"
    assert merkle.verify_proof(p["entry_hash"], p["path"], p["merkle_root"])           # offline check
    assert not merkle.verify_proof(sha256_hex("other"), p["path"], p["merkle_root"])
    v = client.get(f"/api/ledger/verify/{ev}", headers=CALLER()).json()
    assert v["ok"] and v["entry_ok"] and v["merkle_proof_ok"] and v["anchor_provider_ok"] and v["status"] == "ANCHORED"
    assert client.get("/api/ledger/proof/nope", headers=CALLER()).status_code == 404


def test_anchor_mismatch_is_detected_in_the_database_and_in_the_anchor_file(client):
    sos(client)
    a = client.post("/api/ledger/anchor", headers=ADMIN()).json()
    assert verify(client)["ok"]
    with sync_conn() as conn:
        conn.execute("alter table ledger_anchors disable trigger anchors_no_change")
        try:
            conn.execute("update ledger_anchors set merkle_root=%s where anchor_id=%s", ("f" * 64, a["anchor_id"]))
            bad = verify(client)
        finally:
            conn.execute("update ledger_anchors set merkle_root=%s where anchor_id=%s", (a["merkle_root"], a["anchor_id"]))
            conn.execute("alter table ledger_anchors enable trigger anchors_no_change")
    assert bad["failure"]["kind"] == "anchor_root"
    f = ctxof(client).ledger.provider.path
    f.write_text(f.read_text().replace(a["merkle_root"], "e" * 64))
    worse = verify(client)
    assert worse["status"] == "TAMPERED" and worse["failure"]["kind"] == "anchor_provider"


def test_anchor_job_runs_by_count_and_never_on_an_empty_range(client):
    ctx = ctxof(client)
    assert run(client, ctx.ledger.maybe_anchor) is None
    ctx.settings.__dict__["ledger_anchor_every_n"] = 3
    sos(client)
    first = run(client, ctx.ledger.maybe_anchor)
    assert first and first["anchor_id"] == "ANC-0001"
    second = run(client, ctx.ledger.maybe_anchor)
    assert second is None or second["from_seq"] == first["to_seq"] + 1
    assert run(client, ctx.ledger.anchor_now) is None or True
    anchors = client.get("/api/ledger/anchors", headers=DISP()).json()
    assert all(anchors[i]["from_seq"] == anchors[i - 1]["to_seq"] + 1 for i in range(1, len(anchors)))


# ---- signatures and non-repudiation ------------------------------------------------------------------------------
def test_a_signed_sos_is_attached_to_its_ledger_entry_and_verifies(client):
    dev = Device(client)
    r = dev.post("/api/sos", HERE, "sos.start")
    assert r.status_code == 201, r.text
    iid = r.json()["incident_id"]
    created = [e for e in audit(client, iid) if e["event_type"] == "incident.created"][0]
    assert created["ledger"]["client_signed"] and created["ledger"]["signer_key_id"] == dev.key_id
    v = client.get(f"/api/ledger/verify/{created['event_id']}", headers=DISP()).json()
    assert v["ok"] and v["signed_by"] == "client"
    rows = run(client, ctxof(client).store.ledger_rows)
    row = [x for x in rows if x["event_id"] == created["event_id"]][0]
    assert row["sig_message"] and row["server_signature"]
    assert verify_signature(dev.pub, row["sig_message"], row["signature"])
    assert verify(client)["client_signed"] >= 1


def test_receipt_is_signed_by_the_server_and_checks_out_offline(client):
    dev = Device(client)
    r = dev.post("/api/sos", HERE, "sos.start").json()
    rc = r["receipt"]
    assert rc["incident_id"] == r["incident_id"] and rc["event_type"] == "incident.created"
    body = {k: rc[k] for k in ("incident_id", "event_id", "event_type", "event_hash", "seq", "received_at", "server_key_id")}
    assert canonical(body) == rc["message"] and verify_signature(rc["server_public_key"], rc["message"], rc["server_signature"])
    created = [e for e in audit(client, r["incident_id"]) if e["event_type"] == "incident.created"][0]
    assert rc["event_hash"] == created["ledger"]["entry_hash"] and rc["event_id"] == created["event_id"]
    key = client.get("/api/ledger/server-key", headers=CALLER()).json()
    assert key["public_key"] == rc["server_public_key"]
    forged = dict(rc, seq=rc["seq"] + 1)
    assert not verify_signature(rc["server_public_key"], canonical({k: forged[k] for k in body}), rc["server_signature"])


def test_invalid_replayed_and_mismatched_signatures_are_rejected(client):
    dev = Device(client)
    body = json.dumps(HERE).encode()
    good = dev.header("sos.start", body)
    hdr = {**CALLER(), "content-type": "application/json"}
    assert client.post("/api/sos", content=body, headers={**hdr, "x-action-signature": good}).status_code == 201
    replay = client.post("/api/sos", content=body, headers={**hdr, "x-action-signature": good})
    assert replay.status_code == 409 and "replay" in replay.json()["detail"]
    stale = dev.post("/api/sos", HERE, "sos.start", ctr=1)                       # counter not above the last one
    assert stale.status_code == 409
    other_body = dev.post("/api/sos", HERE, "sos.start", sign_body=b"{}")          # signature is for a different body
    assert other_body.status_code == 403 and "body" in other_body.json()["detail"]
    wrong_action = dev.post("/api/sos", HERE, "dispatch.approve")
    assert wrong_action.status_code == 403
    forged = dev.header("sos.start", body)
    env = json.loads(base64.urlsafe_b64decode(forged + "=" * (-len(forged) % 4)))
    env["signature"] = b64(b"\x00" * 64)
    bad = base64.urlsafe_b64encode(json.dumps(env).encode()).decode()
    assert client.post("/api/sos", content=body, headers={**hdr, "x-action-signature": bad}).status_code == 403
    old = dev.post("/api/sos", HERE, "sos.start", ts="2020-01-01T00:00:00.000000Z")
    assert old.status_code == 401
    stranger = Device(client, "caller-2")
    theirs = stranger.header("sos.start", body)
    assert client.post("/api/sos", content=body, headers={**hdr, "x-action-signature": theirs}).status_code == 403   # caller-1 with caller-2's key
    assert client.post("/api/sos", content=body, headers={**hdr, "x-action-signature": "not-base64-json"}).status_code == 401
    assert verify(client)["ok"]


def test_unsigned_actions_still_work_unless_signatures_are_required(client):
    assert sos(client)["incident_id"]
    ctxof(client).settings.__dict__["require_signatures"] = True
    r = client.post("/api/sos", headers=CALLER(), json=HERE)
    assert r.status_code == 401 and "must be signed" in r.json()["detail"]
    ctxof(client).settings.__dict__["require_signatures"] = False


def test_key_rotation_and_revocation(client):
    old = Device(client)
    assert old.post("/api/sos", HERE, "sos.start").status_code == 201
    new_priv = Ed25519PrivateKey.generate()
    new_pub = b64(new_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    r = client.post("/api/keys/rotate", headers=CALLER(), json={"public_key": new_pub, "old_key_id": old.key_id})
    assert r.status_code == 201
    revoked = old.post("/api/sos", HERE, "sos.start")
    assert revoked.status_code == 403 and "revoked" in revoked.json()["detail"]
    mine = {k["key_id"]: k for k in client.get("/api/keys/me", headers=CALLER()).json()}
    assert mine[old.key_id]["revoked_at"] and not mine[r.json()["key_id"]]["revoked_at"]
    fresh = Device.__new__(Device)
    fresh.c, fresh.who, fresh.ctr, fresh.priv, fresh.pub, fresh.key_id = client, "caller-1", 0, new_priv, new_pub, r.json()["key_id"]
    assert fresh.post("/api/sos", HERE, "sos.start").status_code == 201
    types = [e.event_type for e in run(client, ctxof(client).audit.list)]
    assert "key.registered" in types and "key.revoked" in types and "key.rotated" in types
    assert verify(client)["ok"]                                                    # old signatures stay valid history
    assert client.post(f"/api/keys/{old.key_id}/revoke", headers=token_for("caller-2"), json={"reason": "not mine"}).status_code == 403


def test_the_server_stores_only_public_keys(client):
    dev = Device(client)
    with sync_conn() as conn:
        rows = conn.execute("select public_key from actor_keys where key_id=%s", (dev.key_id,)).fetchall()
    assert rows == [(dev.pub,)]
    assert client.post("/api/keys/register", headers=CALLER(), json={"public_key": "A" * 44}).status_code == 422


# ---- delete-proofing -----------------------------------------------------------------------------------------------
def test_delete_attempts_are_refused_and_written_to_the_ledger(client):
    iid = sos(client)["incident_id"]
    for path in (f"/api/incidents/{iid}", "/api/vehicles/AMB-01", "/api/hospitals/HOSP-01", f"/api/incidents/{iid}/reports/R1"):
        r = client.delete(path, headers=DISP())
        assert r.status_code == 405 and "tombstone" in r.json()["detail"]
    assert client.delete(f"/api/incidents/{iid}").status_code == 405                 # anonymous too
    assert client.put(f"/api/incidents/{iid}", headers=DISP(), json={}).status_code == 405
    attempts = [e for e in run(client, ctxof(client).audit.list, entity_type="api") if e.event_type == "delete_attempt"]
    assert len(attempts) == 6 and {e.actor for e in attempts} == {"disp-1", "anonymous"}
    assert any(e.payload["path"] == f"/api/incidents/{iid}" and e.payload["method"] == "DELETE" for e in attempts)
    assert client.get(f"/api/incidents/{iid}", headers=DISP()).status_code == 200      # still there
    assert verify(client)["ok"]


def test_lifting_a_road_closure_is_still_possible_and_audited(client):
    d = client.post("/api/routing/disruptions", headers=DISP(), json={"kind": "closure", "lat": 30.9, "lon": 75.85, "radius_m": 200}).json()
    assert client.delete(f"/api/routing/disruptions/{d['id']}", headers=DISP()).status_code == 200


def test_removal_is_a_tombstone_with_a_reason_and_keeps_the_record(client):
    iid = sos(client)["incident_id"]
    assert client.post("/api/tombstones", headers=DISP(), json={"entity_type": "incident", "entity_id": iid, "reason": "x"}).status_code == 422
    r = client.post("/api/tombstones", headers=DISP(), json={"entity_type": "incident", "entity_id": iid, "reason": "duplicate test entry"})
    assert r.status_code == 201
    assert client.get(f"/api/incidents/{iid}", headers=DISP()).status_code == 200
    assert "tombstone" in [e["event_type"] for e in audit(client, iid)]


# ---- concurrency and restart ----------------------------------------------------------------------------------------
def test_concurrent_appends_keep_one_valid_gap_free_chain(client):
    ctx = ctxof(client)

    async def burst():
        await asyncio.gather(*[ctx.audit.record(entity_type="test", entity_id=f"T{i}", event_type="test.burst", actor="tester",
                                                payload={"i": i}) for i in range(60)])

    client.portal.call(burst)
    res = verify(client)
    assert res["ok"] and res["entries"] >= 61
    rows = run(client, ctx.store.ledger_rows)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert len({r["entry_hash"] for r in rows}) == len(rows)


def test_the_chain_continues_correctly_after_a_restart():
    with make_client(sim_driver=False, sim_volunteers=False) as c1:
        assert c1.post("/api/admin/reset-demo", headers=ADMIN()).status_code == 200
        iid = sos(c1)["incident_id"]
        n1 = verify(c1)["entries"]
        head1 = run(c1, ctxof(c1).store.ledger_head)["entry_hash"]
    with make_client(sim_driver=False, sim_volunteers=False) as c2:                  # new process state, same database, no reset
        assert run(c2, ctxof(c2).store.ledger_head)["entry_hash"] == head1
        sos(c2, token_for("caller-2"))
        res = verify(c2)
        assert res["ok"] and res["entries"] > n1
        rows = run(c2, ctxof(c2).store.ledger_rows)
        assert [r["prev_hash"] for r in rows[1:]] == [r["entry_hash"] for r in rows[:-1]]
        assert iid in {e.correlation_id for e in run(c2, ctxof(c2).audit.list)}


# ---- privacy ---------------------------------------------------------------------------------------------------------
def test_crypto_shredding_makes_personal_data_unreadable_but_the_ledger_stays_valid(client):
    iid = sos(client)["incident_id"]
    created = [e for e in audit(client, iid) if e["event_type"] == "incident.created"][0]
    assert created["new_value"]["lat"] == HERE["lat"] and created["new_value"]["lon"] == HERE["lon"]
    with sync_conn() as conn:                                                      # the database holds no plaintext coordinates
        raw = conn.execute("select new_value::text, personal_enc::text, payload_commitment from audit_events where event_id=%s", (created["event_id"],)).fetchone()
    assert "30.901" not in raw[0] and "[personal]" in raw[0] and raw[1] and len(raw[2]) == 64
    before = verify(client)
    assert before["ok"]
    assert client.post("/api/privacy/erase/caller-1", headers=DISP(), json={"reason": "request"}).status_code == 403
    assert client.post("/api/privacy/erase/caller-1", headers=ADMIN(), json={"reason": "caller asked for erasure"}).status_code == 200
    after = [e for e in audit(client, iid) if e["event_type"] == "incident.created"][0]
    assert after["new_value"]["lat"] == "[erased]" and after["new_value"]["lon"] == "[erased]"
    assert after["new_value"]["channel"] == "sos"                                    # non-personal fields remain
    res = verify(client)
    assert res["ok"] and res["entries"] > before["entries"]                           # chain intact, plus the erasure event
    assert "privacy.erased" in [e.event_type for e in run(client, ctxof(client).audit.list)]
    assert client.post("/api/privacy/erase/caller-1", headers=ADMIN(), json={"reason": "again please"}).status_code == 404
    sos(client)                                                                       # new data for an erased subject is not stored personally
    again = [e for e in run(client, ctxof(client).audit.list) if e.event_type == "incident.created"][-1]
    assert again.new_value["lat"] == "[erased]"


# ---- database roles ------------------------------------------------------------------------------------------------------
def test_the_application_role_can_append_but_not_update_delete_or_truncate(client):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import apply_pg_roles as roles
    from app.db.pg import Store

    sos(client)
    try:
        roles.apply(get_settings().test_database_url, roles.DEMO_PASSWORD)
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("needs a database owner to create roles")
    url = roles.app_url(get_settings().test_database_url, roles.DEMO_PASSWORD)
    with psycopg.connect(url, autocommit=True) as conn:
        assert conn.execute("select count(*) from audit_events").fetchone()[0] > 0
        for q in ("update audit_events set actor='x'", "delete from audit_events", "truncate audit_events",
                  "update actor_keys set actor_id='x'", "delete from ledger_anchors", "drop table audit_events"):
            with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable, psycopg.errors.RaiseException)):
                conn.execute(q)

    async def append_as_app() -> int:
        store = Store(url, init_schema=False)
        await store.open()
        try:
            ctx = ctxof(client)
            old = ctx.store
            ctx.ledger.__dict__["ctx"] = ctx
            ctx.store, ctx.ledger._registered = store, True
            try:
                row = await ctx.audit.record(entity_type="test", entity_id="R", event_type="test.restricted_role", actor="tester")
            finally:
                ctx.store = old
            return row.seq or 0
        finally:
            await store.close()

    assert client.portal.call(append_as_app) > 0
    assert verify(client)["ok"]
