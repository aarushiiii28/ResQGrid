import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useAuth } from './auth'
import type { WsEvent } from './types'

export type LinkState = 'connecting' | 'live' | 'reconnecting' | 'offline'

interface EventsCtx {
  link: LinkState
  lastSeq: number
  lastEventAt: number | null
  /** increments when the server says the gap is too big: reload REST state */
  resyncKey: number
  subscribe: (fn: (e: WsEvent) => void) => () => void
}

const Ctx = createContext<EventsCtx>({
  link: 'offline',
  lastSeq: 0,
  lastEventAt: null,
  resyncKey: 0,
  subscribe: () => () => {},
})
export const useEvents = () => useContext(Ctx)

export function backoff(attempt: number) {
  return Math.min(10000, 800 * 2 ** attempt)
}

/** WebSocket with reconnect + resync. After a drop it reconnects with `since=<last seq>`;
 * the server replays what was missed or tells us to reload state from REST. */
export function EventsProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth()
  const [link, setLink] = useState<LinkState>('connecting')
  const [lastSeq, setLastSeq] = useState(0)
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)
  const [resyncKey, setResyncKey] = useState(0)
  const subs = useRef(new Set<(e: WsEvent) => void>())
  const seqRef = useRef(0)

  useEffect(() => {
    if (!token) {
      setLink('offline')
      return
    }
    let ws: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let closed = false

    const connect = () => {
      if (closed) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const since = seqRef.current > 0 ? `&since=${seqRef.current}` : ''
      setLink(attempt === 0 ? 'connecting' : 'reconnecting')
      try {
        ws = new WebSocket(`${proto}://${location.host}/ws/events?token=${encodeURIComponent(token)}${since}`)
      } catch {
        schedule()
        return
      }
      ws.onopen = () => {
        attempt = 0
        setLink('live')
      }
      ws.onmessage = (m) => {
        let e: WsEvent
        try {
          e = JSON.parse(m.data)
        } catch {
          return
        }
        if (e.event_type === 'sync.resync_required') {
          seqRef.current = e.seq
          setResyncKey((k) => k + 1)
          return
        }
        if (e.event_type === 'sync.hello') {
          if (seqRef.current === 0) seqRef.current = e.seq
          return
        }
        if (e.seq > seqRef.current) seqRef.current = e.seq
        setLastSeq(seqRef.current)
        setLastEventAt(Date.now())
        subs.current.forEach((fn) => fn(e))
      }
      ws.onclose = (ev) => {
        if (closed) return
        if (ev.code === 4401) {
          setLink('offline')
          return
        }
        schedule()
      }
      ws.onerror = () => ws?.close()
    }

    const schedule = () => {
      if (closed) return
      setLink('reconnecting')
      attempt += 1
      timer = setTimeout(connect, backoff(attempt))
    }

    connect()
    return () => {
      closed = true
      clearTimeout(timer)
      ws?.close()
    }
  }, [token])

  const subscribe = useCallback((fn: (e: WsEvent) => void) => {
    subs.current.add(fn)
    return () => {
      subs.current.delete(fn)
    }
  }, [])

  const value = useMemo(
    () => ({ link, lastSeq, lastEventAt, resyncKey, subscribe }),
    [link, lastSeq, lastEventAt, resyncKey, subscribe],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

/** Run `refetch` when a relevant event arrives (debounced), after a resync, and on a slow poll. */
export function useLiveRefresh(refetch: () => void, opts: { pollMs?: number; filter?: (e: WsEvent) => boolean } = {}) {
  const { subscribe, resyncKey, link } = useEvents()
  const ref = useRef(refetch)
  ref.current = refetch
  const filter = useRef(opts.filter)
  filter.current = opts.filter
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | undefined
    const off = subscribe((e) => {
      if (filter.current && !filter.current(e)) return
      clearTimeout(t)
      t = setTimeout(() => ref.current(), 250)
    })
    return () => {
      off()
      clearTimeout(t)
    }
  }, [subscribe])
  useEffect(() => {
    ref.current()
  }, [resyncKey])
  useEffect(() => {
    const ms = opts.pollMs ?? (link === 'live' ? 15000 : 4000)
    const id = setInterval(() => ref.current(), ms)
    return () => clearInterval(id)
  }, [opts.pollMs, link])
}
