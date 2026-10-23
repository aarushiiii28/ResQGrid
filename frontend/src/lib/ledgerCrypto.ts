// Offline-checkable ledger crypto (M19): canonical JSON, SHA-256, Ed25519, receipts and Merkle proofs.
// Everything here runs in the browser with no server round trip, so a receipt kept on the phone can be checked later.
import * as ed from '@noble/ed25519'
import { sha256 } from '@noble/hashes/sha2.js'
import { sha512 } from '@noble/hashes/sha2.js'

// noble/ed25519 needs a SHA-512 implementation for the synchronous API
ed.hashes.sha512 = sha512

const enc = new TextEncoder()

export const toHex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
export const fromHex = (h: string): Uint8Array => {
  if (h.length % 2 || /[^0-9a-f]/i.test(h)) throw new Error('not hex')
  return Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16))
}
export function b64(bytes: Uint8Array): string {
  let s = ''
  for (const x of bytes) s += String.fromCharCode(x)
  return btoa(s)
}
export function unb64(s: string): Uint8Array {
  const bin = atob(s)
  return Uint8Array.from(bin, (c) => c.charCodeAt(0))
}
export function b64url(s: string): string {
  return btoa(String.fromCharCode(...enc.encode(s))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Same bytes as the server's canonical(): sorted keys, no whitespace, UTF-8 characters kept. */
export function canonical(v: unknown): string {
  if (v === null || typeof v !== 'object') return JSON.stringify(v)
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']'
  const o = v as Record<string, unknown>
  return '{' + Object.keys(o).sort().map((k) => JSON.stringify(k) + ':' + canonical(o[k])).join(',') + '}'
}

export const sha256Hex = (data: string | Uint8Array) => toHex(sha256(typeof data === 'string' ? enc.encode(data) : data))

export function publicKeyOf(secret: Uint8Array): Uint8Array {
  return ed.getPublicKey(secret)
}
export function sign(secret: Uint8Array, message: string): string {
  return b64(ed.sign(enc.encode(message), secret))
}
export function verifySig(publicKeyB64: string, message: string, signatureB64: string): boolean {
  try {
    return ed.verify(unb64(signatureB64), enc.encode(message), unb64(publicKeyB64))
  } catch {
    return false
  }
}
export async function keyIdFor(publicKeyB64: string): Promise<string> {
  return 'k-' + sha256Hex(unb64(publicKeyB64)).slice(0, 16)
}

// ---- receipts ----------------------------------------------------------------------------------------------
export interface Receipt {
  incident_id: string
  event_id: string
  event_type: string
  event_hash: string
  seq: number
  received_at: string
  server_key_id: string
  message: string
  server_signature: string
  server_public_key: string
  note?: string
}

export interface ReceiptCheck {
  ok: boolean
  signature: boolean
  message_matches: boolean
  reasons: string[]
}

/** Verify a receipt offline: the server's signature over the exact receipt fields. `trustedKey` (the server key you got
 * from a source you trust) takes priority over the key printed inside the receipt, which proves nothing by itself. */
export function verifyReceipt(r: Receipt, trustedKey?: string): ReceiptCheck {
  const reasons: string[] = []
  const body = {
    incident_id: r.incident_id,
    event_id: r.event_id,
    event_type: r.event_type,
    event_hash: r.event_hash,
    seq: r.seq,
    received_at: r.received_at,
    server_key_id: r.server_key_id,
  }
  const message_matches = canonical(body) === r.message
  if (!message_matches) reasons.push('the receipt fields do not match the signed message (edited?)')
  const key = trustedKey ?? r.server_public_key
  const signature = verifySig(key, r.message, r.server_signature)
  if (!signature) reasons.push('the server signature does not verify')
  if (trustedKey && trustedKey !== r.server_public_key) reasons.push('the key inside the receipt differs from the trusted server key')
  return { ok: message_matches && signature && reasons.length === 0, signature, message_matches, reasons }
}

// ---- Merkle ----------------------------------------------------------------------------------------------------
export interface ProofStep {
  hash: string
  position: 'left' | 'right'
}
const concat = (...a: Uint8Array[]) => {
  const out = new Uint8Array(a.reduce((n, x) => n + x.length, 0))
  let i = 0
  for (const x of a) {
    out.set(x, i)
    i += x.length
  }
  return out
}
export function verifyMerkleProof(entryHashHex: string, path: ProofStep[], rootHex: string): boolean {
  try {
    let node = sha256(concat(Uint8Array.of(0), fromHex(entryHashHex)))
    for (const s of path) {
      const sib = fromHex(s.hash)
      node = s.position === 'left' ? sha256(concat(Uint8Array.of(1), sib, node)) : sha256(concat(Uint8Array.of(1), node, sib))
    }
    return toHex(node) === rootHex
  } catch {
    return false
  }
}
