import type { Receipt } from './ledgerCrypto'

const KEY = 'resqgrid.receipts'
export interface StoredReceipt extends Receipt {
  stored_at: string
}

export function listReceipts(): StoredReceipt[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? '[]') as StoredReceipt[]
  } catch {
    return []
  }
}

/** Keep the server-signed receipt on this device, newest first. Safe to call with anything. */
export function saveReceipt(r: unknown) {
  const x = r as Partial<Receipt> | null
  if (!x || typeof x !== 'object' || !x.event_id || !x.server_signature || !x.message) return
  try {
    const all = listReceipts().filter((y) => y.event_id !== x.event_id)
    all.unshift({ ...(x as Receipt), stored_at: new Date().toISOString() })
    localStorage.setItem(KEY, JSON.stringify(all.slice(0, 50)))
  } catch {
    /* storage unavailable: the receipt is also in the API response */
  }
}

export function removeReceipt(eventId: string) {
  try {
    localStorage.setItem(KEY, JSON.stringify(listReceipts().filter((r) => r.event_id !== eventId)))
  } catch {
    /* ignore */
  }
}
