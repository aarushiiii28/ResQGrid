// Thin fetch client. The Vite dev server proxies /api and /ws to the FastAPI backend.
import { signatureHeader } from './keys'
import { saveReceipt } from './receipts'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
  get offline() {
    return this.status === 0
  }
}

let token: string | null = null
export const setToken = (t: string | null) => {
  token = t
}
export const getToken = () => token

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  const bodyText = body !== undefined ? JSON.stringify(body) : undefined
  // important actions carry a signature made on this device (when a key is unlocked); the server verifies it
  const sig = signatureHeader(method, path, bodyText ?? '')
  try {
    res = await fetch(path, {
      method,
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(sig ? { 'X-Action-Signature': sig } : {}),
      },
      body: bodyText,
    })
  } catch {
    throw new ApiError(0, 'Server unreachable. Check that the API is running.')
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') ?? ''
  if (!ct.includes('json')) return res.text() as Promise<T>
  const data = await res.json()
  if (data && typeof data === 'object' && 'receipt' in data) saveReceipt((data as { receipt: unknown }).receipt) // signed receipt for the caller's device
  return data as T
}

export const api = {
  get: <T>(p: string) => request<T>('GET', p),
  post: <T>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}),
  patch: <T>(p: string, b?: unknown) => request<T>('PATCH', p, b ?? {}),
  del: <T>(p: string) => request<T>('DELETE', p),
}

/** Download an authenticated file (audit export). */
export async function download(path: string, filename: string) {
  const res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!res.ok) throw new ApiError(res.status, 'Export failed')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36)
