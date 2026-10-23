# Tamper-evident ledger (M19)

Everything is simulated demo data. This is a prototype that shows the mechanism. It is **not** a legal evidence system.

## What it gives you
| Claim | How |
|---|---|
| An event was not changed | Each entry holds `entry_hash = SHA-256(prev_hash || canonical fields)`. Changing any field breaks that entry's hash. |
| An event was not removed, inserted or reordered | `seq` is gap-free and every entry names the hash of the one before it. |
| Who did it | Important actions carry an Ed25519 signature made on the actor's device. The server stores only public keys. |
| "I never received it" is false | A signed acknowledgment or approval is a ledger entry anyone can verify. |
| History was not rewritten wholesale | Merkle roots are anchored outside the database (a hash-chained local file by default, optionally an EVM chain). |
| Personal data can be erased | Personal values are encrypted per subject; destroying the key makes them unreadable while the chain stays valid. |

## One ledger
`audit_events` **is** the ledger. `AuditService.record()` writes through `LedgerService.append()`, so the M10 timeline,
the passport and the CSV/JSON exports all read the same rows (personal values decrypted, or `[erased]`).

Entry fields: `seq`, `event_id`, `correlation_id`, `type`, `actor_id`, `occurred_at`, `payload_commitment`, `prev_hash`,
`entry_hash`, `signature`, `signer_key_id`, plus `sig_message` and `server_signature` for device-signed entries and the
anchor (derived from the anchor ranges; `ANCHORED`, `SIGNED` or `PENDING`).

* Canonical JSON: sorted keys, no whitespace, UTF-8, fixed-width UTC timestamps.
* Appends run under a transaction-level advisory lock: strictly sequential and gap-free, also under concurrency. After a restart the
  chain continues from the last stored hash.
* `payload_commitment = SHA-256(salt || canonical(event body))`. The salt is random per entry.

## Signatures
* Browser: `@noble/ed25519`. The key pair is generated on the device; the private key is stored in IndexedDB, encrypted with a key derived
  from a passphrase (PBKDF2, 210 000 rounds, SHA-256, then AES-GCM). It is unlocked once at login, never per action.
  **Demo passphrase**: derived from the user id so the demo works with no setup. A real deployment needs a user-chosen passphrase or a
  platform secure store (Keychain, Android Keystore, WebAuthn).
* Server: `actor_keys` is append-only; registration, rotation and revocation are separate events. A revoked key cannot sign new actions;
  old signatures stay valid history.
* Signed actions: SOS start/confirm, assignment acknowledge/accept/reject, pre-alert acknowledge/arrival/handover, dispatch approval, risk
  recovery approval, driver arrival, reroute accept. The client signs `{action, actor_id, key_id, body_hash, nonce, ctr, ts}` where
  `body_hash` is the SHA-256 of the exact request body. The server checks the key, the signature, the body, the clock (5 minutes), and
  refuses a repeated nonce or a counter that is not higher (replay protection). Unsigned calls still work unless `REQUIRE_SIGNATURES=true`.
* Server-originated events are signed by the server key (`server-1`, kept in `.runtime/`, git-ignored, or `LEDGER_SERVER_KEY`).

## Anchoring
`maybe_anchor()` runs in the watchdog loop: every `LEDGER_ANCHOR_EVERY_N` new entries (default 25) or when the oldest unanchored entry is
`LEDGER_ANCHOR_EVERY_MIN` minutes old (default 10). It builds a Merkle tree over the new entry hashes (leaf `0x00`, node `0x01`) and stores
`{anchor_id, from_seq, to_seq, merkle_root, anchored_at, provider, reference}` in `ledger_anchors` (append-only).

| Provider | Needs | What it is |
|---|---|---|
| `local` (default) | nothing | Also appends to `.runtime/local_anchors.jsonl`, a hash-chained file outside the database. **Local anchor, not a public blockchain.** Whoever controls the machine controls the file. |
| `evm` | `pip install -e ".[evm]"`, a node and a funded **test** key | `contracts/ResQGridAnchor.sol` stores a root and its block time. Works with Anvil/Hardhat or Polygon Amoy / Sepolia. Deploy with `scripts/deploy_anchor_contract.py`. |
| `fabric` | not implemented | Interface stub, see [FABRIC.md](FABRIC.md). |

`GET /api/ledger/proof/{event_id}` returns the sibling path; `GET /api/ledger/verify/{event_id}` checks hash, signature, proof and provider.

## Receipts
On SOS the caller's device receives `{incident_id, event_id, event_type, event_hash, seq, received_at, server_key_id}` signed by the server. It
is kept on the device (and exportable). The **Verify receipt** page checks the signature in the browser, with no server round trip, and
checks a Merkle proof once the event is anchored. A key printed inside a receipt proves nothing on its own; use a server key you trust.

## Delete-proofing
* The API has no DELETE route for incidents, vehicles, hospitals or reports. Any `DELETE` (or `PUT`) under `/api/` returns **405** and
  appends a `delete_attempt` entry with the actor. The one exception is lifting a road closure, which is itself audited.
* "Removal" is a tombstone: `POST /api/tombstones` with a reason. The record stays.
* Database: `audit_events`, `actor_keys` and `ledger_anchors` have triggers that refuse `UPDATE` and `DELETE` for everyone, the owner included.
  `scripts/pg_init_roles.sql` (or `scripts/apply_pg_roles.py`) creates `resqgrid_app`, which can only `INSERT` and `SELECT` on the ledger
  tables: no update, delete, truncate or drop. Connect the app as that role with `DB_SKIP_SCHEMA=true`; the owner creates the schema.
  Resetting the demo (`TRUNCATE`) needs the owner. That is the point.
  (The original spec says MongoDB; this build uses PostgreSQL, so roles and triggers are the Postgres equivalent.)

## Privacy and erasure
Caller location values in `incident.created`, `incident.pending_created` and `incident.location_updated` are replaced by `[personal]` in the
stored row and encrypted per subject with AES-GCM; subject keys are wrapped by a master key (`LEDGER_MASTER_KEY`, demo default derived).
`POST /api/privacy/erase/{subject_id}` (admin) destroys the subject key and logs `privacy.erased`. The values show `[erased]` from then on. The
chain still verifies because it holds only commitments and ciphertext. A commitment of an erased value can no longer be re-checked against its
plaintext, which is the intended result.

## Verify Integrity page
`POST /api/ledger/verify` streams NDJSON progress and a final result: `VERIFIED`, or `TAMPERED at seq N` with expected and found values.
The **Tamper demo** modifies, deletes, reorders, inserts or truncates a throw-away copy in memory and shows the detection. It never touches the
real chain; the response states `real_chain_untouched`.

## Limits
* Tail truncation of the chain is only detected once an anchor covers it.
* A local anchor is not an independent witness. Use the EVM provider (or a real consortium chain) for third-party proof.
* Client-side keys in browser storage need a secure store in production; the demo passphrase is not a secret.
* The server key is a single point of trust for server-originated events.
* Clock skew beyond 5 minutes makes signed actions fail by design.
* Prototype heuristics and synthetic data throughout. Not legal evidence.
