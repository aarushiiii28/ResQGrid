import { useCallback, useEffect, useRef, useState } from 'react'

export interface Fix {
  lat: number
  lon: number
  accuracy_m: number | null
  at: number // epoch ms the position was taken
  source: 'gps' | 'last_known' | 'manual_pin' | 'default'
  approximate: boolean
  speed_ms?: number | null // device-reported ground speed, used on the device for crash detection
}

export type GpsState = 'idle' | 'asking' | 'ok' | 'denied' | 'unavailable' | 'timeout'

const LAST_KEY = 'resqgrid.lastfix'
export const DEFAULT_CENTER = { lat: 30.901, lon: 75.8573 } // Ludhiana (configurable on the server)

export function loadLastFix(): Fix | null {
  try {
    const raw = localStorage.getItem(LAST_KEY)
    if (!raw) return null
    const f = JSON.parse(raw) as Fix
    return { ...f, source: 'last_known', approximate: true }
  } catch {
    return null
  }
}

function saveLastFix(f: Fix) {
  try {
    localStorage.setItem(LAST_KEY, JSON.stringify(f))
  } catch {
    /* ignore */
  }
}

/** Continuous high-accuracy position. Call `start()` on a user gesture. */
export function useWatchPosition() {
  const [fix, setFix] = useState<Fix | null>(null)
  const [state, setState] = useState<GpsState>('idle')
  const watchId = useRef<number | null>(null)

  const stop = useCallback(() => {
    if (watchId.current !== null && navigator.geolocation) navigator.geolocation.clearWatch(watchId.current)
    watchId.current = null
  }, [])

  const start = useCallback(() => {
    if (!navigator.geolocation) {
      setState('unavailable')
      return
    }
    setState('asking')
    watchId.current = navigator.geolocation.watchPosition(
      (p) => {
        const f: Fix = {
          lat: p.coords.latitude,
          lon: p.coords.longitude,
          accuracy_m: p.coords.accuracy ?? null,
          at: p.timestamp || Date.now(),
          source: 'gps',
          approximate: false,
          speed_ms: p.coords.speed ?? null,
        }
        saveLastFix(f)
        setFix(f)
        setState('ok')
      },
      (err) => {
        setState(err.code === 1 ? 'denied' : err.code === 3 ? 'timeout' : 'unavailable')
      },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 10000 },
    )
  }, [])

  useEffect(() => stop, [stop])
  return { fix, state, start, stop }
}

export function fixAgeSeconds(f: Fix | null, now = Date.now()): number {
  return f ? Math.max(0, (now - f.at) / 1000) : 0
}
