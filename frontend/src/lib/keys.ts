// Device signing keys (M19). An Ed25519 key pair is generated on this device. The private key is stored in IndexedDB
// encrypted with a key derived from a passphrase (PBKDF2, 210 000 rounds, SHA-256, then AES-GCM), and unlocked ONCE at
// login: no passphrase prompt on every action. The server only ever receives the public key.
//
// DEMO: the passphrase is derived from the user id so the demo works with no setup, and the UI says so. A real
// deployment needs a user-chosen passphrase or a platform secure store (Keychain, Android Keystore, WebAuthn).
import * as ed from '@noble/ed25519'
import { b64, b64url, canonical, keyIdFor, publicKeyOf, sha256Hex, sign } from './ledgerCrypto'

const DB = 'resqgrid-keys'
const STORE = 'keys'
const ITER = 210_000

interface Stored {
  userId: string
  keyId: string
  publicKey: string
  salt: string
  iv: string
  ct: string
}

interface Unlocked {
  userId: string
  keyId: string
  publicKey: string
  secret: Uint8Array
}

let unlocked: Unlocked | null = null
let lastCtr = 0
export type SignerState = 'none' | 'unlocked' | 'unavailable'
let state: SignerState = 'none'

export const signerState = () => state
export const signerInfo = () => (unlocked ? { keyId: unlocked.keyId, publicKey: unlocked.publicKey, userId: unlocked.userId } : null)
export const demoPassphrase = (userId: string) => `resqgrid-demo-passphrase:${userId}`

function open(): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    if (typeof indexedDB === 'undefined') return rej(new Error('IndexedDB unavailable'))
    const r = indexedDB.open(DB, 1)
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'userId' })
    r.onsuccess = () => res(r.result)
    r.onerror = () => rej(r.error)
  })
}
async function getStored(userId: string): Promise<Stored | null> {
  const db = await open()
  return new Promise((res, rej) => {
    const q = db.transaction(STORE).objectStore(STORE).get(userId)
    q.onsuccess = () => res((q.result as Stored) ?? null)
    q.onerror = () => rej(q.error)
  })
}
async function putStored(s: Stored): Promise<void> {
  const db = await open()
  return new Promise((res, rej) => {
    const q = db.transaction(STORE, 'readwrite').objectStore(STORE).put(s)
    q.onsuccess = () => res()
    q.onerror = () => rej(q.error)
  })
}

async function wrapKey(passphrase: string, salt: Uint8Array): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey('raw', new TextEncoder().encode(passphrase), 'PBKDF2', false, ['deriveKey'])
  return crypto.subtle.deriveKey({ name: 'PBKDF2', salt: salt as BufferSource, iterations: ITER, hash: 'SHA-256' }, base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'])
}
const unb = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0))

/** Create the key on first use, or decrypt it. Keeps the secret in memory for this tab only. */
export async function unlockSigner(userId: string, passphrase = demoPassphrase(userId)): Promise<Unlocked | null> {
  if (unlocked?.userId === userId) return unlocked
  unlocked = null
  try {
    if (typeof crypto === 'undefined' || !crypto.subtle) throw new Error('WebCrypto unavailable')
    let rec = await getStored(userId)
    if (!rec) {
      const secret = ed.utils.randomSecretKey()
      const salt = crypto.getRandomValues(new Uint8Array(16))
      const iv = crypto.getRandomValues(new Uint8Array(12))
      const key = await wrapKey(passphrase, salt)
      const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, secret as BufferSource))
      const publicKey = b64(publicKeyOf(secret))
      rec = { userId, keyId: await keyIdFor(publicKey), publicKey, salt: b64(salt), iv: b64(iv), ct: b64(ct) }
      await putStored(rec)
    }
    const key = await wrapKey(passphrase, unb(rec.salt))
    const secret = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unb(rec.iv) as BufferSource }, key, unb(rec.ct) as BufferSource))
    unlocked = { userId, keyId: rec.keyId, publicKey: rec.publicKey, secret }
    state = 'unlocked'
    return unlocked
  } catch {
    state = 'unavailable' // wrong passphrase, no IndexedDB or no WebCrypto: actions go unsigned, everything still works
    return null
  }
}

export function lockSigner() {
  unlocked = null
  state = 'none'
}

/** Strictly increasing counter, so the server can refuse a replayed or reordered signature. */
function nextCtr(): number {
  const t = Math.floor((performance.timeOrigin + performance.now()) * 1000)
  lastCtr = Math.max(lastCtr + 1, t)
  return lastCtr
}

const RULES: [RegExp, string][] = [
  [/^\/api\/sos$/, 'sos.start'],
  [/^\/api\/sos\/start$/, 'sos.start'],
  [/^\/api\/incidents\/[^/]+\/sos\/confirm$/, 'sos.confirm'],
  [/^\/api\/assignments\/[^/]+\/(delivered|acknowledge|accept|reject)$/, 'assignment.$1'],
  [/^\/api\/prealerts\/[^/]+\/(acknowledge|arrival|handover_started|handover_completed)$/, 'prealert.$1'],
  [/^\/api\/dispatch\/[^/]+\/approve$/, 'dispatch.approve'],
  [/^\/api\/risks\/[^/]+\/recover$/, 'risk.recover'],
  [/^\/api\/driver\/[^/]+\/arrived$/, 'driver.arrived'],
  [/^\/api\/driver\/[^/]+\/reroute\/[^/]+\/(accept|decline)$/, 'reroute.$1'],
  [/^\/api\/driver\/[^/]+\/route-notice\/[^/]+\/ack$/, 'reroute.ack'],
]

export function actionFor(method: string, path: string): string | null {
  if (method !== 'POST') return null
  for (const [re, action] of RULES) {
    const m = path.match(re)
    if (m) return action.replace('$1', m[1] ?? '')
  }
  return null
}

/** Header value for X-Action-Signature, or null when no key is unlocked or the call is not an important action. */
export function signatureHeader(method: string, path: string, bodyText: string): string | null {
  const action = actionFor(method, path)
  if (!action || !unlocked) return null
  const message = canonical({
    v: 1,
    action,
    actor_id: unlocked.userId,
    key_id: unlocked.keyId,
    body_hash: sha256Hex(bodyText),
    nonce: crypto.randomUUID?.() ?? Math.random().toString(36).slice(2) + Date.now().toString(36),
    ctr: nextCtr(),
    ts: new Date().toISOString().replace('Z', '000Z'), // microsecond-style, parsed by the server
  })
  return b64url(JSON.stringify({ key_id: unlocked.keyId, message, signature: sign(unlocked.secret, message) }))
}
