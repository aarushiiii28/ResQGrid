import 'fake-indexeddb/auto'
import { describe, expect, it } from 'vitest'
import { actionFor, lockSigner, signatureHeader, signerInfo, signerState, unlockSigner } from './keys'
import { b64, canonical, sha256Hex, unb64, verifyMerkleProof, verifyReceipt, verifySig } from './ledgerCrypto'
import { V } from '../test/vectors'
import { listReceipts, removeReceipt, saveReceipt } from './receipts'

// jsdom may hide Node's WebCrypto; the app needs crypto.subtle (PBKDF2, AES-GCM)
if (!globalThis.crypto?.subtle) {
  const mod = 'node:crypto'
  const { webcrypto } = await import(/* @vite-ignore */ mod)
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto })
}

describe('browser crypto matches the Python server', () => {
  it('canonical JSON is byte-identical (sorted keys, unicode, escapes)', () => {
    expect(canonical(V.canon_in)).toBe(V.canon_out)
  })

  it('verifies a receipt signed by the server, offline', () => {
    const c = verifyReceipt(V.receipt)
    expect(c).toMatchObject({ ok: true, signature: true, message_matches: true })
    expect(verifyReceipt(V.receipt, V.receipt.server_public_key).ok).toBe(true)
  })

  it('rejects an edited receipt, a bad signature and a different trusted key', () => {
    expect(verifyReceipt({ ...V.receipt, seq: 13 }).message_matches).toBe(false)
    expect(verifyReceipt({ ...V.receipt, server_signature: b64(new Uint8Array(64)) }).signature).toBe(false)
    const other = b64(new Uint8Array(32).fill(7))
    expect(verifyReceipt(V.receipt, other).ok).toBe(false)
  })

  it('verifies a Merkle inclusion proof built by the server and rejects a wrong leaf or root', () => {
    expect(verifyMerkleProof(V.leaf, V.proof, V.root)).toBe(true)
    expect(verifyMerkleProof(sha256Hex('forged'), V.proof, V.root)).toBe(false)
    expect(verifyMerkleProof(V.leaf, V.proof, 'ff'.repeat(32))).toBe(false)
    expect(verifyMerkleProof('nothex', [], V.root)).toBe(false)
  })
})

describe('device signing keys', () => {
  it('maps important actions, and nothing else, to signed action names', () => {
    expect(actionFor('POST', '/api/sos')).toBe('sos.start')
    expect(actionFor('POST', '/api/sos/start')).toBe('sos.start')
    expect(actionFor('POST', '/api/incidents/INC-1001/sos/confirm')).toBe('sos.confirm')
    expect(actionFor('POST', '/api/assignments/ASG-0001/accept')).toBe('assignment.accept')
    expect(actionFor('POST', '/api/prealerts/PA-1/handover_completed')).toBe('prealert.handover_completed')
    expect(actionFor('POST', '/api/dispatch/DEC-1/approve')).toBe('dispatch.approve')
    expect(actionFor('POST', '/api/driver/AMB-01/reroute/RR-1/accept')).toBe('reroute.accept')
    expect(actionFor('GET', '/api/sos')).toBeNull()
    expect(actionFor('POST', '/api/prealerts/PA-1/checklist')).toBeNull()
    expect(actionFor('POST', '/api/incidents')).toBeNull()
  })

  it('generates a key once, stores only an encrypted private key, and signs headers the server format expects', async () => {
    lockSigner()
    expect(signatureHeader('POST', '/api/sos', '{}')).toBeNull() // locked: unsigned, still works
    const k = await unlockSigner('caller-1')
    expect(k && signerState()).toBe('unlocked')
    expect(k!.keyId).toMatch(/^k-[0-9a-f]{16}$/)
    const again = await unlockSigner('caller-1')
    expect(again!.publicKey).toBe(k!.publicKey)
    lockSigner()
    const reopened = await unlockSigner('caller-1') // decrypts the stored key with the passphrase
    expect(reopened!.publicKey).toBe(k!.publicKey)
    const body = JSON.stringify({ lat: 30.9, lon: 75.85 })
    const h = signatureHeader('POST', '/api/sos', body)!
    const env = JSON.parse(atob(h.replace(/-/g, '+').replace(/_/g, '/')))
    const m = JSON.parse(env.message)
    expect(m).toMatchObject({ v: 1, action: 'sos.start', actor_id: 'caller-1', key_id: k!.keyId, body_hash: sha256Hex(body) })
    expect(verifySig(k!.publicKey, env.message, env.signature)).toBe(true)
    expect(unb64(env.signature)).toHaveLength(64)
    expect(m.ts).toMatch(/\.\d{6}Z$/)
  })

  it('counters strictly increase and every nonce is fresh', async () => {
    await unlockSigner('caller-1')
    const ms = Array.from({ length: 20 }, () => {
      const h = signatureHeader('POST', '/api/sos', '{}')!
      return JSON.parse(JSON.parse(atob(h.replace(/-/g, '+').replace(/_/g, '/'))).message) as { ctr: number; nonce: string }
    })
    expect(ms.map((m) => m.ctr)).toEqual([...ms.map((m) => m.ctr)].sort((a, b) => a - b))
    expect(new Set(ms.map((m) => m.ctr)).size).toBe(20)
    expect(new Set(ms.map((m) => m.nonce)).size).toBe(20)
  })

  it('a different user gets a different key, and the private key is not stored in the clear', async () => {
    const a = await unlockSigner('caller-1')
    const b = await unlockSigner('disp-1')
    expect(a!.publicKey).not.toBe(b!.publicKey)
    expect(signerInfo()!.userId).toBe('disp-1')
    const db: IDBDatabase = await new Promise((res) => {
      const r = indexedDB.open('resqgrid-keys', 1)
      r.onsuccess = () => res(r.result)
    })
    const rec: Record<string, string> = await new Promise((res) => {
      const q = db.transaction('keys').objectStore('keys').get('disp-1')
      q.onsuccess = () => res(q.result)
    })
    expect(Object.keys(rec).sort()).toEqual(['ct', 'iv', 'keyId', 'publicKey', 'salt', 'userId'])
    expect(rec.ct).not.toContain(b!.publicKey)
  })
})

describe('receipts kept on the device', () => {
  it('saves a signed receipt once, newest first, and can remove it', () => {
    saveReceipt(V.receipt)
    saveReceipt({ ...V.receipt, event_id: 'other-event' })
    saveReceipt(V.receipt)
    saveReceipt({ nothing: true })
    expect(listReceipts().map((r) => r.event_id)).toEqual([V.receipt.event_id, 'other-event'])
    removeReceipt('other-event')
    expect(listReceipts()).toHaveLength(1)
  })
})