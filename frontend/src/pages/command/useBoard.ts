import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import { useEvents, useLiveRefresh } from '../../lib/events'
import type { AlertItem, Assignment, Decision, Disruption, FaView, HospitalView, Incident, VehicleView } from '../../lib/types'

export interface Board {
  falseAlarms: FaView[]
  incidents: Incident[]
  vehicles: VehicleView[]
  hospitals: HospitalView[]
  alerts: AlertItem[]
  decisions: Decision[]
  assignments: Assignment[]
  disruptions: Disruption[]
  autopilot: { enabled: boolean; delay_s: number; label: string } | null
  loadedAt: number | null
  error: string | null
  loading: boolean
}

const EMPTY: Board = {
  falseAlarms: [],
  incidents: [],
  vehicles: [],
  hospitals: [],
  alerts: [],
  decisions: [],
  assignments: [],
  disruptions: [],
  autopilot: null,
  loadedAt: null,
  error: null,
  loading: true,
}

/** The whole operations picture, refreshed when events arrive and on a slow poll. Failures keep the
 * last good data on screen and say so, so a dropped connection never shows invented live data. */
export function useBoard() {
  const [board, setBoard] = useState<Board>(EMPTY)
  const busy = useRef(false)

  const refresh = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const [incidents, vehicles, hospitals, alerts, decisions, assignments, disruptions, autopilot, falseAlarms] = await Promise.all([
        api.get<Incident[]>('/api/incidents'),
        api.get<VehicleView[]>('/api/vehicles'),
        api.get<HospitalView[]>('/api/hospitals'),
        api.get<AlertItem[]>('/api/alerts'),
        api.get<Decision[]>('/api/dispatch/decisions'),
        api.get<Assignment[]>('/api/assignments'),
        api.get<Disruption[]>('/api/routing/disruptions'),
        api.get<Board['autopilot']>('/api/dispatch/autopilot'),
        api.get<FaView[]>('/api/false-alarm').catch(() => [] as FaView[]), // the board still works without it
      ])
      setBoard({ incidents, vehicles, hospitals, alerts, decisions, assignments, disruptions, autopilot, falseAlarms, loadedAt: Date.now(), error: null, loading: false })
    } catch (e) {
      setBoard((b) => ({ ...b, error: (e as Error).message, loading: false }))
    } finally {
      busy.current = false
    }
  }, [])

  const { subscribe } = useEvents()
  // position pings patch the map directly (smooth movement) instead of refetching everything
  useEffect(
    () =>
      subscribe((e) => {
        if (e.event_type !== 'vehicle.location_updated') return
        const p = e.payload as { lat: number; lon: number }
        setBoard((b) => ({
          ...b,
          vehicles: b.vehicles.map((v) =>
            v.id === e.entity_id
              ? { ...v, location: v.location ? { ...v.location, lat: p.lat, lon: p.lon } : v.location, location_state: 'live', location_age_s: 0, last_update_age_s: 0 }
              : v,
          ),
        }))
      }),
    [subscribe],
  )
  useLiveRefresh(refresh, { pollMs: 5000, filter: (e) => e.event_type !== 'vehicle.location_updated' })
  return { board, refresh }
}

export function latestDecision(decisions: Decision[], incidentId: string): Decision | null {
  const mine = decisions.filter((d) => d.incident_id === incidentId)
  return mine.length ? mine[mine.length - 1] : null
}
