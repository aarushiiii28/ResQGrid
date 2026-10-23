import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'

type Handler = (body: unknown, url: string) => unknown | Promise<unknown>

export interface ApiMock {
  calls: { method: string; path: string; body: unknown }[]
  set: (key: string, h: Handler) => void
  count: (method: string, pathStart: string) => number
  find: (method: string, pathStart: string) => { method: string; path: string; body: unknown }[]
}

/** Replaces fetch with a router keyed "METHOD /path" (query ignored). Unknown routes return 404. */
export function mockApi(routes: Record<string, Handler | unknown> = {}): ApiMock {
  const table = new Map<string, Handler>()
  const calls: ApiMock['calls'] = []
  const toHandler = (v: Handler | unknown): Handler => (typeof v === 'function' ? (v as Handler) : () => v)
  Object.entries(routes).forEach(([k, v]) => table.set(k, toHandler(v)))
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const path = url.split('?')[0]
      const method = (init?.method ?? 'GET').toUpperCase()
      let body: unknown
      try {
        body = init?.body ? JSON.parse(String(init.body)) : undefined
      } catch {
        body = init?.body
      }
      calls.push({ method, path, body })
      const h = table.get(`${method} ${path}`) ?? [...table.entries()].find(([k]) => k.endsWith('*') && `${method} ${path}`.startsWith(k.slice(0, -1)))?.[1]
      if (!h) return new Response(JSON.stringify({ detail: `no mock for ${method} ${path}` }), { status: 404, headers: { 'content-type': 'application/json' } })
      const out = await h(body, url)
      if (out instanceof Response) return out
      return new Response(JSON.stringify(out ?? {}), { status: 200, headers: { 'content-type': 'application/json' } })
    }),
  )
  return {
    calls,
    set: (k, h) => table.set(k, toHandler(h)),
    count: (m, p) => calls.filter((c) => c.method === m && c.path.startsWith(p)).length,
    find: (m, p) => calls.filter((c) => c.method === m && c.path.startsWith(p)),
  }
}

export class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  onopen: (() => void) | null = null
  onmessage: ((m: { data: string }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
    setTimeout(() => this.onopen?.(), 0)
  }
  close() {
    this.closed = true
    this.onclose?.({ code: 1000 })
  }
  emit(ev: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(ev) })
  }
  drop() {
    this.onclose?.({ code: 1006 })
  }
}

export function installWebSocket() {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
}

export function ev(type: string, extra: Record<string, unknown> = {}) {
  return {
    event_id: Math.random().toString(36).slice(2),
    event_type: type,
    occurred_at: new Date().toISOString(),
    entity_id: 'X',
    correlation_id: 'INC-1001',
    source: 'system',
    payload: {},
    seq: 1,
    ...extra,
  }
}

/** Press and hold the SOS control past the (test-shortened) hold time, as a person would. */
export async function holdSos(holdMs = 160) {
  const user = userEvent.setup()
  const btn = screen.getByRole('button', { name: /SOS/ })
  await user.pointer({ keys: '[MouseLeft>]', target: btn })
  await new Promise((r) => setTimeout(r, holdMs))
  await user.pointer({ keys: '[/MouseLeft]' })
}

export function signIn(user: Record<string, unknown>) {
  sessionStorage.setItem('resqgrid.session', JSON.stringify({ token: 'test-token', user }))
}

export function geolocation(mode: 'ok' | 'denied', coords = { latitude: 30.9, longitude: 75.85, accuracy: 14 }) {
  const watch = vi.fn((ok: (p: unknown) => void, err: (e: { code: number }) => void) => {
    setTimeout(() => {
      if (mode === 'ok') ok({ coords, timestamp: Date.now() })
      else err({ code: 1 })
    }, 10)
    return 1
  })
  Object.defineProperty(globalThis.navigator, 'geolocation', { configurable: true, value: { watchPosition: watch, clearWatch: vi.fn(), getCurrentPosition: vi.fn() } })
  return watch
}
