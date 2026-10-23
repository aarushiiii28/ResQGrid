"""M19 hybrid ledger. audit_events IS the ledger: every audit/domain event is appended here, hash-chained
(entry_hash = SHA-256(prev_hash || canonical fields)), signed (Ed25519), and periodically anchored by a Merkle root
to a provider outside the database. Personal payloads are encrypted per subject so they can be crypto-shredded
without breaking the chain (the ledger holds only salted commitments).

Prototype. See docs/LEDGER.md for what this does and does not prove."""

import copy
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.security import Principal
from app.services import merkle
from app.services.anchor import AnchorProvider, build_anchor_provider
from app.services.crypto_service import (
    GENESIS,
    SERVER_KEY_ID,
    ServerKey,
    Vault,
    b64url_decode,
    canonical,
    iso,
    key_id_for,
    sha256_hex,
    valid_public_key,
    verify_signature,
)

PERSONAL_PLACEHOLDER = "[personal]"
ERASED = "[erased]"
# events whose location fields are personal data and are encrypted per subject (the caller)
PERSONAL_EVENTS = {"incident.created", "incident.pending_created", "incident.location_updated"}
PERSONAL_KEYS = ("lat", "lon", "accuracy_m")
CLOCK_SKEW_S = 300


@dataclass
class SignedAction:
    """A verified client signature, waiting to be attached to the first matching ledger entry of this request."""

    sub: str
    key_id: str
    message: str
    signature: str
    action: str
    event_types: tuple[str, ...]
    used: bool = field(default=False)


CURRENT_SIGNED: ContextVar[SignedAction | None] = ContextVar("current_signed_action", default=None)


def _matches(event_type: str, patterns: tuple[str, ...]) -> bool:
    return any(event_type == p or (p.endswith("*") and event_type.startswith(p[:-1])) for p in patterns)


def entry_fields(row: dict) -> dict:
    """The fields the entry hash covers. Anything not listed here is not protected by the hash."""
    sig_digest = sha256_hex(f"{row['sig_message']}|{row['signature']}") if row.get("sig_message") else None
    at = row["occurred_at"]
    return {
        "seq": row["seq"], "event_id": row["event_id"], "correlation_id": row.get("correlation_id"),
        "type": row["event_type"], "actor_id": row["actor"], "occurred_at": iso(at) if isinstance(at, datetime) else at,
        "payload_commitment": row["payload_commitment"], "signer_key_id": row["signer_key_id"], "signature_digest": sig_digest,
    }


def compute_entry_hash(prev_hash: str, fields: dict) -> str:
    return sha256_hex(prev_hash + canonical(fields))


# ---- pure chain verification (used live, by the streaming verifier and by the sandbox tamper demo) -----------
@dataclass
class Failure:
    seq: int
    kind: str
    expected: Any = None
    found: Any = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "expected": self.expected, "found": self.found, "detail": self.detail}


class ChainVerifier:
    """Feeds ledger rows in order and stops at the first problem. `keys` maps key_id -> {public_key, revoked_at}."""

    def __init__(self, keys: dict[str, dict], server_public: str):
        self.keys, self.server_public = keys, server_public
        self.prev = GENESIS
        self.expected_seq = 1
        self.entries = 0
        self.legacy = 0
        self.client_signed = 0
        self.server_signed = 0
        self.hashes: dict[int, str] = {}

    def _key(self, key_id: str) -> str | None:
        if key_id == SERVER_KEY_ID:
            return self.server_public
        k = self.keys.get(key_id)
        return k["public_key"] if k else None

    def _check_key_state(self, key_id: str, row: dict) -> Failure | None:
        k = self.keys.get(key_id)
        if k and k.get("revoked_at") and row["occurred_at"] > k["revoked_at"] + timedelta(seconds=2):
            return Failure(row["seq"], "revoked_key", detail=f"{key_id} was revoked before this entry")
        return None

    def feed(self, row: dict) -> Failure | None:
        seq = row["seq"]
        if not row.get("entry_hash"):
            if self.entries:
                return Failure(seq, "unchained_entry", detail="an entry without a hash appeared inside the chain")
            self.legacy += 1
            self.expected_seq = seq + 1
            return None
        if seq != self.expected_seq:
            return Failure(self.expected_seq, "seq_gap", expected=self.expected_seq, found=seq,
                           detail="an entry is missing, moved or inserted")
        if row["prev_hash"] != self.prev:
            return Failure(seq, "prev_hash", expected=self.prev, found=row["prev_hash"], detail="the link to the previous entry is broken")
        expected = compute_entry_hash(row["prev_hash"], entry_fields(row))
        if expected != row["entry_hash"]:
            return Failure(seq, "entry_hash", expected=expected, found=row["entry_hash"], detail="the entry was changed after it was written")
        # signatures
        if row.get("sig_message"):
            pub = self._key(row["signer_key_id"])
            if pub is None:
                return Failure(seq, "unknown_key", found=row["signer_key_id"])
            bad = self._check_key_state(row["signer_key_id"], row)
            if bad:
                return bad
            if not verify_signature(pub, row["sig_message"], row["signature"]):
                return Failure(seq, "signature_invalid", detail=f"client signature by {row['signer_key_id']} does not verify")
            try:
                if json.loads(row["sig_message"]).get("actor_id") != row["actor"]:
                    return Failure(seq, "signature_actor", detail="the signed message names a different actor")
            except ValueError:
                return Failure(seq, "signature_invalid", detail="signed message is not JSON")
            if not row.get("server_signature") or not verify_signature(self.server_public, row["entry_hash"], row["server_signature"]):
                return Failure(seq, "signature_invalid", detail="server countersignature does not verify")
            self.client_signed += 1
        else:
            pub = self._key(row["signer_key_id"])
            if pub is None:
                return Failure(seq, "unknown_key", found=row["signer_key_id"])
            if not row.get("signature") or not verify_signature(pub, row["entry_hash"], row["signature"]):
                return Failure(seq, "signature_invalid", detail=f"signature by {row['signer_key_id']} does not verify")
            self.server_signed += 1
        self.prev = row["entry_hash"]
        self.expected_seq = seq + 1
        self.entries += 1
        self.hashes[seq] = row["entry_hash"]
        return None

    def check_anchors(self, anchors: list[dict], provider: AnchorProvider | None) -> tuple[int, Failure | None]:
        checked = 0
        for a in anchors:
            rng = [self.hashes.get(s) for s in range(a["from_seq"], a["to_seq"] + 1)]
            if any(h is None for h in rng):
                return checked, Failure(a["to_seq"], "anchor_truncated", expected=a["to_seq"],
                                        detail=f"{a['anchor_id']} covers entries that are no longer in the ledger")
            root = merkle.merkle_root([h for h in rng if h])
            if root != a["merkle_root"]:
                return checked, Failure(a["from_seq"], "anchor_root", expected=a["merkle_root"], found=root,
                                        detail=f"{a['anchor_id']}: entries no longer match the anchored root")
            if provider is not None:
                r = provider.verify(a["anchor_id"], a["merkle_root"], a.get("reference") or {})
                if not r.ok:
                    return checked, Failure(a["from_seq"], "anchor_provider", detail=f"{a['anchor_id']}: {r.detail}")
            checked += 1
        return checked, None


class LedgerService:
    def __init__(self, ctx):
        self.ctx = ctx
        s = ctx.settings
        self.runtime = Path(s.ledger_runtime_dir) if s.ledger_runtime_dir else Path(__file__).resolve().parents[3] / ".runtime"
        key_file = None if s.ledger_server_key else self.runtime / "ledger_server_ed25519.key"
        self.server = ServerKey(key_file, s.ledger_server_key or None)
        self.vault = Vault(s.ledger_master_key or None, s.jwt_secret)
        self.provider: AnchorProvider = build_anchor_provider(s, self.runtime)
        self._registered = False
        self._unanchored_since: datetime | None = None

    @property
    def store(self):
        return self.ctx.store

    def reset_runtime(self) -> None:
        """Demo reset: the database ledger is wiped, so the local anchor file (which mirrors it) is wiped too."""
        self.provider.reset()
        self._registered = False
        self._unanchored_since = None

    # ---- appending ------------------------------------------------------------------------------------
    async def _ensure_server_key(self) -> None:
        if self._registered:
            return
        self._registered = True
        added = await self.store.add_key_event(SERVER_KEY_ID, "server", "registered", self.server.public_b64, "ledger server key")
        if added:
            await self._append(
                {"event_id": str(uuid.uuid4()), "correlation_id": None, "entity_type": "key", "entity_id": SERVER_KEY_ID,
                 "event_type": "key.registered", "occurred_at": self.ctx.now(), "actor": "server", "source": "system",
                 "reason": "ledger server key", "old_value": None, "new_value": None,
                 "payload": {"key_id": SERVER_KEY_ID, "public_key": self.server.public_b64, "algo": "ed25519"}})

    async def append(self, event: dict, *, personal: dict | None = None, subject_id: str | None = None) -> dict:
        await self._ensure_server_key()
        return await self._append(event, personal=personal, subject_id=subject_id)

    async def _append(self, event: dict, *, personal: dict | None = None, subject_id: str | None = None) -> dict:
        salt = os.urandom(16).hex()
        body = {k: event.get(k) for k in ("entity_type", "entity_id", "source", "reason", "old_value", "new_value")}
        body["payload"] = event.get("payload") or {}
        # the commitment covers the REAL values; personal values are swapped for a placeholder before storing
        full = copy.deepcopy(body)
        for part, vals in (personal or {}).items():
            if isinstance(full.get(part), dict):
                full[part].update(vals)
        commitment = sha256_hex(salt + canonical(full))
        enc = None
        if personal and subject_id:
            key = await self._subject_key(subject_id, create=True)
            if key is not None:
                enc = Vault.encrypt(key, personal, aad=event["event_id"])
            else:  # subject already erased: store nothing personal at all
                personal = None
        ev = {**event, "salt": salt, "payload_commitment": commitment, "personal_enc": enc, "subject_id": subject_id if enc else None}
        signed = CURRENT_SIGNED.get()
        use_client = bool(signed and not signed.used and signed.sub == event["actor"] and _matches(event["event_type"], signed.event_types))

        def make(seq: int, prev: str) -> dict:
            row = {**ev, "seq": seq, "prev_hash": prev}
            row["signer_key_id"] = signed.key_id if use_client else SERVER_KEY_ID
            if use_client:
                row["sig_message"], row["signature"] = signed.message, signed.signature
            fields = entry_fields({**row, "sig_message": row.get("sig_message"), "signature": row.get("signature")})
            row["entry_hash"] = compute_entry_hash(prev, fields)
            if use_client:
                row["server_signature"] = self.server.sign(row["entry_hash"])
            else:
                row["signature"], row["sig_message"], row["server_signature"] = self.server.sign(row["entry_hash"]), None, None
            return row

        row = await self.store.append_chained(make)
        if use_client:
            signed.used = True
        if self._unanchored_since is None:
            self._unanchored_since = row["occurred_at"]
        return row

    # ---- reading ---------------------------------------------------------------------------------------
    async def hydrate(self, row: dict) -> dict:
        """Put decrypted personal values back (or '[erased]' after crypto-shredding)."""
        row = dict(row)
        key = await self._subject_key(row["subject_id"], create=False) if row.get("subject_id") else None
        data = Vault.decrypt(key, row["personal_enc"], row["event_id"]) if key and row.get("personal_enc") else None
        for part in ("old_value", "new_value", "payload"):
            cur = row.get(part)
            if not isinstance(cur, dict):
                continue
            cur = dict(cur)
            for k, v in list(cur.items()):
                if v == PERSONAL_PLACEHOLDER:
                    cur[k] = (data or {}).get(part, {}).get(k, ERASED) if data else ERASED
            row[part] = cur
        return row

    async def _subject_key(self, subject_id: str, *, create: bool) -> bytes | None:
        row = await self.store.get_subject_key(subject_id)
        if row is None:
            if not create:
                return None
            key, wrapped = self.vault.new_subject_key()
            await self.store.create_subject_key(subject_id, wrapped)
            row = await self.store.get_subject_key(subject_id)
            if row and row["wrapped_key"]:
                return self.vault.unwrap(row["wrapped_key"])
            return key
        if row["destroyed_at"] is not None or not row["wrapped_key"]:
            return None
        return self.vault.unwrap(row["wrapped_key"])

    async def erase_subject(self, subject_id: str, actor: str, reason: str) -> dict:
        """Crypto-shredding: destroy the subject's key. Their personal values become unreadable for good; the ledger
        stays verifiable because it only ever held commitments and ciphertext."""
        destroyed = await self.store.destroy_subject_key(subject_id)
        if not destroyed:
            raise HTTPException(404, "no readable personal data for this subject")
        await self.ctx.audit.record(entity_type="subject", entity_id=subject_id, event_type="privacy.erased", actor=actor,
                                    source="human_confirmed", reason=reason, payload={"subject": subject_id, "method": "crypto-shredding",
                                                                                      "note": "key destroyed; ledger commitments unchanged"})
        return {"subject_id": subject_id, "erased": True}

    # ---- actor keys ---------------------------------------------------------------------------------------
    async def keys_snapshot(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in await self.store.key_events():
            k = out.setdefault(e["key_id"], {"key_id": e["key_id"], "actor_id": e["actor_id"], "public_key": None, "registered_at": None, "revoked_at": None})
            if e["event"] == "registered":
                k["public_key"], k["registered_at"] = e["public_key"], e["at"]
            else:
                k["revoked_at"], k["revoke_reason"] = e["at"], e["reason"]
        return out

    async def list_keys(self, actor_id: str | None = None) -> list[dict]:
        snap = await self.keys_snapshot()
        return [k for k in snap.values() if k["actor_id"] != "server" and (actor_id is None or k["actor_id"] == actor_id)]

    async def register_key(self, actor_id: str, public_key: str, by: str) -> dict:
        if not valid_public_key(public_key):
            raise HTTPException(422, "not a valid Ed25519 public key (32 bytes, base64)")
        kid = key_id_for(public_key)
        snap = await self.keys_snapshot()
        if kid in snap and snap[kid]["actor_id"] != actor_id:
            raise HTTPException(409, "this key is already registered to someone else")
        added = await self.store.add_key_event(kid, actor_id, "registered", public_key)
        if added:
            await self.ctx.audit.record(entity_type="key", entity_id=kid, event_type="key.registered", actor=by, source="reported",
                                        payload={"key_id": kid, "actor_id": actor_id, "public_key": public_key, "algo": "ed25519"})
        return {"key_id": kid, "actor_id": actor_id, "registered": added}

    async def revoke_key(self, key_id: str, by: str, reason: str) -> dict:
        snap = await self.keys_snapshot()
        if key_id not in snap or key_id == SERVER_KEY_ID:
            raise HTTPException(404, "unknown key")
        added = await self.store.add_key_event(key_id, snap[key_id]["actor_id"], "revoked", None, reason)
        if added:
            await self.ctx.audit.record(entity_type="key", entity_id=key_id, event_type="key.revoked", actor=by, source="human_confirmed",
                                        reason=reason, payload={"key_id": key_id, "actor_id": snap[key_id]["actor_id"]})
        return {"key_id": key_id, "revoked": added}

    async def rotate_key(self, actor_id: str, new_public_key: str, old_key_id: str | None, by: str) -> dict:
        new = await self.register_key(actor_id, new_public_key, by)
        if old_key_id:
            snap = await self.keys_snapshot()
            if old_key_id in snap and snap[old_key_id]["actor_id"] == actor_id and not snap[old_key_id]["revoked_at"]:
                await self.revoke_key(old_key_id, by, "rotated")
                await self.ctx.audit.record(entity_type="key", entity_id=new["key_id"], event_type="key.rotated", actor=by, source="reported",
                                            payload={"old_key_id": old_key_id, "new_key_id": new["key_id"], "actor_id": actor_id})
        return new

    # ---- verifying signed actions -------------------------------------------------------------------------
    async def verify_action(self, body: bytes, header: str | None, p: Principal, *, action: str, event_types: tuple[str, ...]) -> SignedAction | None:
        """Check the client's signature over this exact request: who, what, when, nonce and counter. Returns None when
        unsigned (allowed unless REQUIRE_SIGNATURES is on)."""
        if not header:
            if self.ctx.settings.require_signatures:
                raise HTTPException(401, f"this action must be signed ({action}); no X-Action-Signature header")
            return None
        try:
            env = json.loads(b64url_decode(header).decode("utf-8"))
            key_id, message, sig = env["key_id"], env["message"], env["signature"]
            m = json.loads(message)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(401, "malformed signature header") from e
        if m.get("v") != 1 or m.get("actor_id") != p.sub or m.get("key_id") != key_id:
            raise HTTPException(403, "the signature names a different actor or key")
        if m.get("action") != action:
            raise HTTPException(403, f"the signature is for '{m.get('action')}', not '{action}'")
        if m.get("body_hash") != sha256_hex(body):
            raise HTTPException(403, "the signature does not match this request body")
        try:
            ts = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError) as e:
            raise HTTPException(401, "missing or bad timestamp") from e
        if abs((self.ctx.now() - ts).total_seconds()) > CLOCK_SKEW_S:
            raise HTTPException(401, "signature timestamp is too old or in the future")
        snap = await self.keys_snapshot()
        k = snap.get(key_id)
        if not k or k["actor_id"] != p.sub or k["public_key"] is None:
            raise HTTPException(403, "this key is not registered to you")
        if k["revoked_at"]:
            raise HTTPException(403, "this key has been revoked")
        if not verify_signature(k["public_key"], message, sig):
            raise HTTPException(403, "signature does not verify")
        nonce, ctr = str(m.get("nonce", "")), m.get("ctr")
        if not nonce or not isinstance(ctr, int):
            raise HTTPException(401, "missing nonce or counter")
        res = await self.store.claim_nonce(key_id, nonce, ctr)
        if res != "ok":
            raise HTTPException(409, f"replayed or out-of-order signature ({res})")
        return SignedAction(sub=p.sub, key_id=key_id, message=message, signature=sig, action=action, event_types=event_types)

    # ---- anchoring ---------------------------------------------------------------------------------------
    async def anchors(self) -> list[dict]:
        return await self.store.list_anchors()

    async def anchor_now(self, *, actor: str = "ledger-anchor") -> dict | None:
        await self._ensure_server_key()
        anchors = await self.store.list_anchors()
        start = (anchors[-1]["to_seq"] + 1) if anchors else 1
        rows = [r for r in await self.store.ledger_rows(from_seq=start) if r.get("entry_hash")]
        if not rows:
            return None
        n = await self.store.next_seq("anchor_seq")
        aid, now = f"ANC-{n:04d}", self.ctx.now()
        root = merkle.merkle_root([r["entry_hash"] for r in rows])
        ref = self.provider.anchor(aid, root, rows[0]["seq"], rows[-1]["seq"], iso(now))
        rec = await self.store.add_anchor({"anchor_id": aid, "from_seq": rows[0]["seq"], "to_seq": rows[-1]["seq"], "merkle_root": root,
                                           "anchored_at": now, "provider": self.provider.name, "reference": ref})
        self._unanchored_since = None
        await self.ctx.audit.record(entity_type="ledger", entity_id=aid, event_type="ledger.anchored", actor=actor, source="system",
                                    payload={"anchor_id": aid, "from_seq": rec["from_seq"], "to_seq": rec["to_seq"], "merkle_root": root,
                                             "provider": self.provider.name, "label": self.provider.label})
        return rec

    async def maybe_anchor(self) -> dict | None:
        s = self.ctx.settings
        head = await self.store.ledger_head()
        if head is None:
            return None
        anchors = await self.store.list_anchors()
        last = anchors[-1]["to_seq"] if anchors else 0
        pending = head["seq"] - last
        if pending <= 0:
            return None
        old = self._unanchored_since is not None and (self.ctx.now() - self._unanchored_since) >= timedelta(minutes=s.ledger_anchor_every_min)
        if pending >= s.ledger_anchor_every_n or old:
            return await self.anchor_now()
        return None

    # ---- proofs & per-event verification -------------------------------------------------------------------
    async def proof(self, event_id: str) -> dict:
        row = await self.store.ledger_row(event_id)
        if row is None or not row.get("entry_hash"):
            raise HTTPException(404, "event not found in the ledger")
        anchors = await self.store.list_anchors()
        a = next((x for x in anchors if x["from_seq"] <= row["seq"] <= x["to_seq"]), None)
        base = {"event_id": event_id, "seq": row["seq"], "entry_hash": row["entry_hash"], "prev_hash": row["prev_hash"]}
        if a is None:
            return {**base, "anchored": False, "status": "PENDING", "note": "not anchored yet: the next anchor will cover it"}
        rows = [r for r in await self.store.ledger_rows(from_seq=a["from_seq"], to_seq=a["to_seq"]) if r.get("entry_hash")]
        hashes = [r["entry_hash"] for r in rows]
        idx = next(i for i, r in enumerate(rows) if r["seq"] == row["seq"])
        return {**base, "anchored": True, "status": "ANCHORED", "anchor": {k: (iso(v) if isinstance(v, datetime) else v) for k, v in a.items()},
                "merkle_root": a["merkle_root"], "path": merkle.merkle_proof(hashes, idx), "leaf_index": idx, "leaves": len(hashes),
                "provider_label": self.provider.label}

    async def verify_event(self, event_id: str) -> dict:
        row = await self.store.ledger_row(event_id)
        if row is None or not row.get("entry_hash"):
            raise HTTPException(404, "event not found in the ledger")
        keys = await self.keys_snapshot()
        v = ChainVerifier(keys, self.server.public_b64)
        v.prev, v.expected_seq = row["prev_hash"], row["seq"]
        fail = v.feed(row)
        proof = await self.proof(event_id)
        proof_ok = None
        provider_ok = None
        if proof["anchored"]:
            proof_ok = merkle.verify_proof(row["entry_hash"], proof["path"], proof["merkle_root"])
            a = proof["anchor"]
            provider_ok = self.provider.verify(a["anchor_id"], proof["merkle_root"], a.get("reference") or {}).ok
        ok = fail is None and proof_ok is not False and provider_ok is not False
        return {"event_id": event_id, "seq": row["seq"], "ok": ok, "entry_ok": fail is None, "failure": fail.as_dict() if fail else None,
                "signed_by": "client" if row.get("sig_message") else "server", "signer_key_id": row["signer_key_id"],
                "anchored": proof["anchored"], "merkle_proof_ok": proof_ok, "anchor_provider_ok": provider_ok, "status": proof["status"]}

    def status_of(self, row: dict, anchors: list[dict]) -> dict:
        """The badge in the Incident Passport: ANCHORED > SIGNED (client) > PENDING (server-attested, waiting for an anchor)."""
        a = next((x for x in anchors if x["from_seq"] <= row["seq"] <= x["to_seq"]), None) if row.get("entry_hash") else None
        client = bool(row.get("sig_message"))
        status = "ANCHORED" if a else ("SIGNED" if client else "PENDING")
        return {"seq": row["seq"], "entry_hash": row.get("entry_hash"), "signer_key_id": row.get("signer_key_id"), "client_signed": client,
                "anchor_id": a["anchor_id"] if a else None, "ledger_status": status}

    # ---- full verification (streaming) ---------------------------------------------------------------------
    async def verify_all(self, chunk: int = 250) -> AsyncIterator[dict]:
        keys = await self.keys_snapshot()
        head = await self.store.ledger_head()
        total = head["seq"] if head else 0
        v = ChainVerifier(keys, self.server.public_b64)
        yield {"type": "start", "total": total}
        seq, failure = 1, None
        while seq <= total and failure is None:
            rows = await self.store.ledger_rows(from_seq=seq, to_seq=seq + chunk - 1)
            if not rows:
                failure = Failure(seq, "seq_gap", expected=seq, found=None, detail="the ledger ends early")
                break
            for r in rows:
                failure = v.feed(r)
                if failure:
                    break
            seq += chunk
            yield {"type": "progress", "phase": "links and signatures", "checked": v.entries + v.legacy, "total": total}
        anchors = await self.store.list_anchors()
        checked = 0
        if failure is None:
            yield {"type": "progress", "phase": "anchors", "checked": 0, "total": len(anchors)}
            checked, failure = v.check_anchors(anchors, self.provider)
            yield {"type": "progress", "phase": "anchors", "checked": checked, "total": len(anchors)}
        yield {"type": "result", **self._result(v, failure, checked, len(anchors))}

    def _result(self, v: ChainVerifier, failure: Failure | None, anchors_ok: int, anchors_total: int) -> dict:
        return {"ok": failure is None, "status": "VERIFIED" if failure is None else "TAMPERED", "entries": v.entries, "legacy_unchained": v.legacy,
                "client_signed": v.client_signed, "server_signed": v.server_signed,
                "anchors": {"checked": anchors_ok, "total": anchors_total, "provider": self.provider.name, "label": self.provider.label},
                "failure": failure.as_dict() if failure else None}

    async def verify_result(self) -> dict:
        out: dict = {}
        async for ev in self.verify_all():
            if ev["type"] == "result":
                out = ev
        return out

    # ---- sandbox tamper demonstration (NEVER touches the real chain) -----------------------------------------
    async def tamper_demo(self, mode: str) -> dict:
        rows = [dict(r) for r in await self.store.ledger_rows() if r.get("entry_hash")]
        if len(rows) < 6:
            raise HTTPException(409, "not enough ledger entries to demonstrate; create an incident first")
        head_before = await self.store.ledger_head()
        anchors = await self.store.list_anchors()
        if mode == "truncate" and not anchors:
            await self.anchor_now(actor="tamper-demo-setup")
            rows = [dict(r) for r in await self.store.ledger_rows() if r.get("entry_hash")]
            anchors = await self.store.list_anchors()
        sandbox = copy.deepcopy(rows)
        mid = len(sandbox) // 2
        what: str
        if mode == "modify":
            sandbox[mid]["actor"] = "someone-else"
            what = f"changed the actor of entry {sandbox[mid]['seq']}"
        elif mode == "delete":
            what = f"deleted entry {sandbox[mid]['seq']}"
            del sandbox[mid]
        elif mode == "reorder":
            sandbox[mid], sandbox[mid + 1] = sandbox[mid + 1], sandbox[mid]
            what = f"swapped entries {sandbox[mid]['seq']} and {sandbox[mid + 1]['seq']}"
        elif mode == "insert":
            forged = copy.deepcopy(sandbox[mid])
            forged["event_id"], forged["seq"], forged["prev_hash"] = str(uuid.uuid4()), sandbox[mid]["seq"] + 1, sandbox[mid]["entry_hash"]
            forged["actor"] = "attacker"
            forged["entry_hash"] = compute_entry_hash(forged["prev_hash"], entry_fields(forged))  # hash recomputed, but no valid signature
            sandbox.insert(mid + 1, forged)
            what = f"inserted a forged entry after {sandbox[mid]['seq']} (hash recomputed, signature not possible)"
        elif mode == "truncate":
            cut = anchors[-1]["to_seq"]
            sandbox = [r for r in sandbox if r["seq"] <= cut - 2]
            what = "removed the last entries (the most recent history)"
        else:
            raise HTTPException(422, "mode must be modify, delete, reorder, insert or truncate")
        keys = await self.keys_snapshot()
        v = ChainVerifier(keys, self.server.public_b64)
        failure = None
        for r in sandbox:
            failure = v.feed(r)
            if failure:
                break
        if failure is None and mode == "truncate":
            _, failure = v.check_anchors(anchors, self.provider)
        # the real chain may legitimately grow while we work; what must not change is anything already written
        again = await self.store.ledger_rows(from_seq=head_before["seq"], to_seq=head_before["seq"])
        untouched = bool(again) and again[0]["entry_hash"] == head_before["entry_hash"]
        return {"mode": mode, "what": what, "detected": failure is not None, "failure": failure.as_dict() if failure else None,
                "sandbox_entries": len(sandbox), "real_chain_untouched": untouched,
                "label": "DEMONSTRATION on a throw-away copy in memory. The real ledger was not changed."}

    # ---- receipts -----------------------------------------------------------------------------------------
    async def receipt(self, incident_id: str, event_type: str) -> dict | None:
        await self._ensure_server_key()
        rows = [r for r in await self.store.ledger_rows() if r.get("correlation_id") == incident_id and r["event_type"] == event_type]
        if not rows:
            return None
        r = rows[0]
        body = {"incident_id": incident_id, "event_id": r["event_id"], "event_type": event_type, "event_hash": r["entry_hash"],
                "seq": r["seq"], "received_at": iso(r["occurred_at"]), "server_key_id": SERVER_KEY_ID}
        message = canonical(body)
        return {**body, "message": message, "server_signature": self.server.sign(message), "server_public_key": self.server.public_b64,
                "note": "Signed by the server. Keep it: it proves the server received this event and where it sits in the ledger."}

    # ---- delete-proofing ------------------------------------------------------------------------------------
    async def record_delete_attempt(self, method: str, path: str, actor: str) -> None:
        await self.ctx.audit.record(entity_type="api", entity_id=path[:200], event_type="delete_attempt", actor=actor, source="system",
                                    payload={"method": method, "path": path, "result": "refused (405): nothing is ever deleted; use a tombstone with a reason"})

    async def tombstone(self, entity_type: str, entity_id: str, reason: str, actor: str) -> dict:
        doc = {"id": f"{entity_type}:{entity_id}", "entity_type": entity_type, "entity_id": entity_id, "reason": reason, "by": actor,
               "at": iso(self.ctx.now())}
        await self.store.put("tombstone", doc["id"], doc)
        await self.ctx.audit.record(entity_type=entity_type, entity_id=entity_id, event_type="tombstone", actor=actor, source="human_confirmed",
                                    correlation_id=entity_id if entity_type == "incident" else None, reason=reason, payload={"note": "removal is a tombstone event; the record itself is kept"})
        return doc
