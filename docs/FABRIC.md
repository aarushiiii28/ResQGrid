# Hyperledger Fabric anchoring (interface stub, not implemented)

`backend/app/services/anchor/fabric_stub.py` defines `FabricAnchorProvider`. It raises `NotImplementedError` and is **not needed to run
ResQGrid**. This page describes what a real implementation would look like so the provider interface can be filled in later.

## What it would anchor
The same thing the local and EVM providers anchor: one Merkle root per range of ledger entries (`anchor_id`, `from_seq`, `to_seq`,
`merkle_root`, `anchored_at`). No personal data and no event payloads ever leave the database.

## Chaincode (Go or TypeScript)
Two functions on a channel such as `resqgrid-anchors`:

| Function | Behaviour |
|---|---|
| `AnchorRoot(anchorId, root, fromSeq, toSeq, anchoredAt)` | Writes `anchorId -> {root, fromSeq, toSeq, anchoredAt, submitterMSP}` to world state. Fails if `anchorId` already exists (an anchor can never be replaced). |
| `GetAnchor(anchorId)` | Returns the stored record. |

State is append-only by policy: no update or delete function exists, and the endorsement policy requires peers from at least two
organisations (for example dispatch authority and hospital network) so no single party can rewrite history.

## Network
- One channel, at least two organisations, two peers each, a Raft ordering service.
- The ResQGrid backend submits through the Fabric Gateway client with an application identity.

## Provider mapping
- `anchor(...)` submits `AnchorRoot` and returns `{"tx_id": ..., "channel": ..., "label": "Hyperledger Fabric"}`.
- `verify(...)` calls `GetAnchor` and compares the stored root with the database root.

Set `LEDGER_ANCHOR_PROVIDER=fabric` only after implementing the two methods. Until then the setting raises on first use by design.
