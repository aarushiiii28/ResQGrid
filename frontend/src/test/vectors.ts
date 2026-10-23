// Vectors produced by the Python server code (crypto_service.py, merkle.py) so the browser is tested against the real thing.
import type { ProofStep, Receipt } from '../lib/ledgerCrypto'

export const V = {"receipt": {"incident_id": "INC-1001", "event_id": "11111111-2222-3333-4444-555555555555", "event_type": "incident.created", "event_hash": "abababababababababababababababababababababababababababababababab", "seq": 12, "received_at": "2026-01-02T03:04:05.000006Z", "server_key_id": "server-1", "message": "{\"event_hash\":\"abababababababababababababababababababababababababababababababab\",\"event_id\":\"11111111-2222-3333-4444-555555555555\",\"event_type\":\"incident.created\",\"incident_id\":\"INC-1001\",\"received_at\":\"2026-01-02T03:04:05.000006Z\",\"seq\":12,\"server_key_id\":\"server-1\"}", "server_signature": "Waji3n7v/TaaGeJzvMbIuSDjBrPkXckPcg5fcIUVLOJBs0em8su7qkcakGZpFwvLpXRHJSBvLSm1TxnURy3zAA==", "server_public_key": "Wg52guBqsoiV935y9i7jryp8eptKbq4OgOMQmjZdQpQ="}, "root": "dbbe28c05c993cb7bc95c9a924366152cd46591d2a0b9a1a4249dc33c9fd019b", "leaf": "449777124b1466a8ed667d0dd4c0620993f59e20fb27b3fa8894e957f8762353", "proof": [{"hash": "bdb9655b2405e0fdd9e815de932d7143289f5e29c547b78596ed757781dda0a6", "position": "right"}, {"hash": "cc60fb9c9f7bed4756fea57d894a6974c7fa37958cf11eb8b7aae19f08769121", "position": "right"}, {"hash": "cc59c76c58b2032ee569337971a5fe0c7f27c40a87d19728d133cfc2eda19210", "position": "left"}], "canon_in": {"b": [1, 2.5, null, true], "a": {"z": "\u00e9\u2603", "y": "q\"\\"}, "n": -3}, "canon_out": "{\"a\":{\"y\":\"q\\\"\\\\\",\"z\":\"\u00e9\u2603\"},\"b\":[1,2.5,null,true],\"n\":-3}"} as {
  receipt: Receipt
  root: string
  leaf: string
  proof: ProofStep[]
  canon_in: unknown
  canon_out: string
}
