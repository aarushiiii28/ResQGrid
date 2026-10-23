import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../lib/api'
import { getTiming, loadTiming, type SosTiming } from '../../lib/alerts'
import { DEFAULT_CENTER, fixAgeSeconds, loadLastFix, useWatchPosition, type Fix } from '../../lib/geo'
import { deviceId } from './useSignals'

export interface Session {
  incident_id: string
  call_id: string
  operator: string
  started_at: number
}

/** idle -> (hold) locating -> countdown -> confirming -> live, or countdown -> cancelling -> cancelled */
export type Phase = 'idle' | 'locating' | 'countdown' | 'confirming' | 'cancelling' | 'live' | 'cancelled'

const KEY = 'resqgrid.caller.active'

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}
export function saveSession(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s))
    else sessionStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}

const GPS_WAIT_MS = 2500

/** The caller holds SOS once. After the hold this hook asks for the position, and as soon as it has a fix (or
 * has waited 2.5 s, or been refused) creates a PENDING incident and the cancel countdown starts. When the
 * countdown ends the request goes live; Cancel keeps the record and queues one check-in. Later, better fixes
 * and pin drags update the same incident. */
export function useSos() {
  const gps = useWatchPosition()
  const [session, setSession] = useState<Session | null>(loadSession)
  const [phase, setPhase] = useState<Phase>(session ? 'live' : 'idle')
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [cancelledId, setCancelledId] = useState<string | null>(null)
  const [timing, setTiming] = useState<SosTiming>(getTiming())
  const [error, setError] = useState<string | null>(null)
  const [pin, setPin] = useState<Fix | null>(null) // manual pin when GPS is refused or weak
  const created = useRef(session !== null)
  const sentFix = useRef<Fix | null>(null)
  const sessionRef = useRef(session)
  sessionRef.current = session
  const pendingRef = useRef<string | null>(null)
  pendingRef.current = pendingId

  useEffect(() => {
    void loadTiming().then(setTiming)
  }, [])

  const create = useCallback(async (fix: Fix) => {
    if (created.current) return
    created.current = true
    sentFix.current = fix
    try {
      const r = await api.post<{ incident_id: string }>('/api/sos/start', {
        lat: fix.lat,
        lon: fix.lon,
        accuracy_m: fix.accuracy_m,
        location_age_s: Math.round(fixAgeSeconds(fix)),
        approximate: fix.approximate,
        location_source: fix.source,
        device_id: deviceId(),
      })
      setPendingId(r.incident_id)
      setPhase('countdown')
    } catch (e) {
      created.current = false
      setPhase('idle')
      setError(e instanceof ApiError ? e.message : 'Could not reach the server.')
    }
  }, [])

  /** Called when the hold completes. */
  const begin = useCallback(() => {
    if (phase !== 'idle') return
    setPhase('locating')
    setError(null)
    gps.start()
  }, [phase, gps])

  // first fix -> create immediately
  useEffect(() => {
    if (phase === 'locating' && gps.fix && !created.current) void create(gps.fix)
  }, [phase, gps.fix, create])

  // refused / unavailable -> still proceed with last known or the city centre, flagged approximate
  useEffect(() => {
    if (phase !== 'locating' || created.current) return
    if (gps.state === 'denied' || gps.state === 'unavailable' || gps.state === 'timeout') {
      const last = loadLastFix()
      const f: Fix = last ?? { ...DEFAULT_CENTER, accuracy_m: null, at: Date.now(), source: 'default', approximate: true }
      setPin(f)
      void create(f)
    }
  }, [phase, gps.state, create])

  // no fix after 2.5 s -> do not make the caller wait
  useEffect(() => {
    if (phase !== 'locating' || created.current) return
    const t = setTimeout(() => {
      if (created.current) return
      const last = loadLastFix()
      const f: Fix = last ?? { ...DEFAULT_CENTER, accuracy_m: null, at: Date.now(), source: 'default', approximate: true }
      setPin(f)
      void create(f)
    }, GPS_WAIT_MS)
    return () => clearTimeout(t)
  }, [phase, create, gps.state])

  /** The countdown reached zero: the request goes live and the simulated call opens. */
  const confirm = useCallback(async () => {
    const id = pendingRef.current
    if (!id) return
    setPhase('confirming')
    try {
      const r = await api.post<{ incident_id: string; call_id: string; operator: string }>(`/api/incidents/${id}/sos/confirm`)
      const s: Session = { ...r, started_at: Date.now() }
      saveSession(s)
      setSession(s)
      setPendingId(null)
      setPhase('live')
    } catch (e) {
      // never leave the caller stuck in a countdown: the server also makes an unconfirmed request live
      setError(e instanceof ApiError ? e.message : 'Could not reach the server.')
      setPhase('countdown')
    }
  }, [])

  const cancel = useCallback(async () => {
    const id = pendingRef.current
    if (!id) return
    setPhase('cancelling')
    try {
      await api.post(`/api/incidents/${id}/sos/cancel`)
      setCancelledId(id)
      setPendingId(null)
      setPhase('cancelled')
      gps.stop()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        void confirm() // the countdown had just ended: the request is live
        return
      }
      setError(e instanceof ApiError ? e.message : 'Could not cancel. Try again.')
      setPhase('countdown')
    }
  }, [confirm, gps])

  const pushLocation = useCallback(async (f: Fix) => {
    const id = sessionRef.current?.incident_id ?? pendingRef.current
    if (!id) return
    try {
      await api.post(`/api/incidents/${id}/location`, {
        lat: f.lat,
        lon: f.lon,
        accuracy_m: f.accuracy_m,
        location_age_s: Math.round(fixAgeSeconds(f)),
        approximate: f.approximate,
        location_source: f.source,
      })
      sentFix.current = f
    } catch {
      /* the next fix will retry */
    }
  }, [])

  // a real GPS fix arriving after an approximate start, or the caller moving, updates the incident
  useEffect(() => {
    const f = gps.fix
    if (!f || !(session || pendingId)) return
    const prev = sentFix.current
    const moved = !prev || Math.hypot((f.lat - prev.lat) * 111320, (f.lon - prev.lon) * 111320 * 0.86) > 20
    const upgraded = prev?.approximate && !f.approximate
    if (moved || upgraded) {
      setPin(null)
      void pushLocation(f)
    }
  }, [gps.fix, session, pendingId, pushLocation])

  const movePin = useCallback(
    (lat: number, lon: number) => {
      const f: Fix = { lat, lon, accuracy_m: null, at: Date.now(), source: 'manual_pin', approximate: true }
      setPin(f)
      void pushLocation(f)
    },
    [pushLocation],
  )

  const reset = useCallback(() => {
    saveSession(null)
    created.current = false
    sentFix.current = null
    gps.stop()
    setSession(null)
    setPendingId(null)
    setCancelledId(null)
    setPhase('idle')
    setPin(null)
    setError(null)
  }, [gps])

  return { session, phase, pressed: phase !== 'idle', pendingId, cancelledId, timing, begin, confirm, cancel, error, gps, pin, movePin, reset, fix: gps.fix }
}
