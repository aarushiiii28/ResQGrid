import type { Marker as LMarker } from 'leaflet'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Circle, Marker, Polyline, Tooltip } from 'react-leaflet'
import MapView from '../../components/MapView'
import { aedIcon, hospitalIcon, pinIcon, sceneIcon, vehicleIcon, youIcon } from '../../components/markers'
import { Ambulance } from '../../components/art'
import { Empty, ErrorBox, Loading, Stamp } from '../../components/ui'
import { api } from '../../lib/api'
import { useEvents, useLiveRefresh } from '../../lib/events'
import { ageText, label } from '../../lib/format'
import type { Aed, Tracker } from '../../lib/types'
import type { Fix } from '../../lib/geo'

interface Props {
  incidentId: string
  fix: Fix | null
  pin: Fix | null
  onMovePin: (lat: number, lon: number) => void
  aeds: Aed[]
  onTracker?: (t: Tracker) => void
}

export default function TrackerPanel({ incidentId, fix, pin, onMovePin, aeds, onTracker }: Props) {
  const [t, setT] = useState<Tracker | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [live, setLive] = useState<{ lat: number; lon: number; at: number } | null>(null)
  const { subscribe, link } = useEvents()

  const load = useCallback(async () => {
    try {
      const r = await api.get<Tracker>(`/api/tracker/${incidentId}`)
      setT(r)
      setErr(null)
      onTracker?.(r)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [incidentId, onTracker])

  useLiveRefresh(load, { filter: (e) => e.correlation_id === incidentId })

  // smooth ambulance movement straight from the WebSocket, without waiting for a refetch
  useEffect(
    () =>
      subscribe((e) => {
        if (e.event_type === 'vehicle.location_updated' && e.correlation_id === incidentId) {
          const p = e.payload as { lat: number; lon: number }
          setLive({ lat: p.lat, lon: p.lon, at: Date.now() })
        }
      }),
    [subscribe, incidentId],
  )

  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const you = useMemo<[number, number] | null>(() => {
    if (pin) return [pin.lat, pin.lon]
    if (fix) return [fix.lat, fix.lon]
    if (t?.location) return [t.location.lat, t.location.lon]
    return null
  }, [pin, fix, t])

  const veh = t?.vehicle
  const vehPos: [number, number] | null = live ? [live.lat, live.lon] : veh ? [veh.lat, veh.lon] : null
  const vehAge = live ? (now - live.at) / 1000 : veh?.location_age_s ?? null
  const hosp = t?.hospital
  const nearby = veh ? [] : (t?.nearby ?? [])
  const fitPts: [number, number][] = [
    you,
    vehPos,
    hosp ? ([hosp.lat, hosp.lon] as [number, number]) : null,
    ...nearby.slice(0, 2).map((n) => [n.lat, n.lon] as [number, number]),
  ].filter(Boolean) as [number, number][]

  if (!t && !err) return <Loading what="Contacting dispatch" />
  if (!t && err) return <ErrorBox message={err} onRetry={load} />
  if (!t) return null

  const approximate = pin !== null || t.location?.approximate

  return (
    <section aria-label="Live status" className="border-2 border-ink bg-paper">
      {link !== 'live' && (
        <div role="status" className="border-b-2 border-ink bg-amber px-3 py-1 font-mono text-xs font-semibold uppercase">
          {link === 'offline' ? 'Offline. Showing the last update.' : 'Reconnecting. Status refreshes every few seconds.'}
        </div>
      )}
      <div className="border-b-2 border-ink px-3 py-3">
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <span className="font-mono text-[11px] uppercase text-ink/70">Status {t.incident_id}</span>
          {t.autopilot.enabled && <Stamp tone="ink" title={t.autopilot.label}>Demo Autopilot on</Stamp>}
          {veh?.simulated && <Stamp tone="ink">Ambulance movement simulated</Stamp>}
          {t.needs_review && <Stamp tone="warn">Call details under review</Stamp>}
        </div>
        <p aria-live="polite" data-testid="headline" className="font-display text-[34px] font-bold uppercase leading-[1.02] sm:text-[44px]">
          {t.headline}
        </p>
        {t.eta && (
          <p className="mt-1 font-mono text-lg">
            ETA <b>{t.eta.text}</b> <span className="text-ink/60">({t.eta.phase === 'scene' ? 'to you' : 'to hospital'})</span>
          </p>
        )}
      </div>

      <div className="grid lg:grid-cols-[1fr_1fr]">
        <div className="relative h-[300px] border-b-2 border-ink lg:h-[380px] lg:border-b-0 lg:border-r-2">
          <MapView
            className="h-full"
            center={you ?? [30.901, 75.857]}
            zoom={15}
            fit={fitPts.length ? fitPts : undefined}
            fitKey={`${incidentId}-${!!vehPos}-${!!hosp}-${nearby.length}`}
            label="Live map of your location and the ambulance"
          >
            {t.route && t.route.polyline.length > 1 && <Polyline positions={t.route.polyline} pathOptions={{ color: '#E4570F', weight: 5, dashArray: '10 8' }} />}
            {you && !approximate && (
              <>
                <Marker position={you} icon={youIcon()}>
                  <Tooltip direction="top">You</Tooltip>
                </Marker>
                {fix?.accuracy_m ? <Circle center={you} radius={fix.accuracy_m} pathOptions={{ color: '#15120E', weight: 1, fillOpacity: 0.08 }} /> : null}
              </>
            )}
            {you && approximate && (
              <Marker
                position={you}
                icon={pinIcon(true)}
                draggable
                eventHandlers={{
                  dragend: (e) => {
                    const ll = (e.target as LMarker).getLatLng()
                    onMovePin(ll.lat, ll.lng)
                  },
                }}
              />
            )}
            {you && !approximate && <Marker position={you} icon={sceneIcon()} interactive={false} />}
            {vehPos && veh && (
              <Marker position={vehPos} icon={vehicleIcon({ id: veh.id, status: veh.status, stale: veh.state === 'stale' && !live, simulated: veh.simulated })}>
                <Tooltip direction="top">
                  {veh.id} · {veh.source} · {ageText(vehAge)} ago
                </Tooltip>
              </Marker>
            )}
            {nearby.map((n) => (
              <Marker key={n.id} position={[n.lat, n.lon]} icon={vehicleIcon({ id: n.id, status: 'available', stale: false, simulated: n.simulated })}>
                <Tooltip direction="top">
                  {n.id} · available · {(n.distance_m / 1000).toFixed(1)} km away · simulated
                </Tooltip>
              </Marker>
            ))}
            {hosp && (
              <Marker position={[hosp.lat, hosp.lon]} icon={hospitalIcon({ stale: false })}>
                <Tooltip direction="top">{hosp.name}</Tooltip>
              </Marker>
            )}
            {aeds.slice(0, 3).map((a, i) => (
              <Marker key={a.id} position={[a.lat, a.lon]} icon={aedIcon(i === 0)}>
                <Tooltip direction="top">
                  {a.label}: {Math.round(a.walk_low_s / 60)}-{Math.round(a.walk_high_s / 60)} min walk
                </Tooltip>
              </Marker>
            ))}
          </MapView>
          {approximate && (
            <div className="absolute bottom-2 left-2 z-[1000] border-2 border-amber bg-ink px-2 py-1 font-mono text-[11px] font-semibold uppercase text-amber">
              Approximate location. Drag the pin to where you are.
            </div>
          )}
        </div>

        <div className="px-3 py-3">
          <ol aria-label="Progress" className="space-y-0">
            {t.stages.map((s) => (
              <li key={s.key} className="flex items-stretch gap-3">
                <div className="flex flex-col items-center">
                  <span
                    aria-hidden="true"
                    className={`mt-1 grid h-5 w-5 place-items-center border-2 border-ink text-[11px] font-bold ${s.state === 'done' ? 'bg-green text-paper' : s.state === 'active' ? 'bg-orange blink' : 'bg-paper'}`}
                  >
                    {s.state === 'done' ? '✓' : ''}
                  </span>
                  <span className="w-0.5 flex-1 bg-ink/30" aria-hidden="true" />
                </div>
                <div className={`pb-3 ${s.state === 'pending' ? 'text-ink/50' : ''}`}>
                  <div className="text-lg font-semibold leading-tight">{s.label}</div>
                  <div className="font-mono text-[11px] uppercase text-ink/60">
                    {s.state === 'done' ? `done ${s.at ? new Date(s.at).toLocaleTimeString([], { hour12: false }) : ''}` : s.state === 'active' ? 'in progress' : 'waiting'}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>

      <div className="grid gap-px border-t-2 border-ink bg-ink/20 sm:grid-cols-3">
        <div className="bg-paper px-3 py-2">
          <div className="font-mono text-[11px] uppercase text-ink/60">Ambulance</div>
          {veh ? (
            <div className="flex items-center gap-2">
              <Ambulance className="h-8 w-auto" moving={veh.status === 'en_route' || veh.status === 'transporting'} />
              <div>
                <div className="font-mono text-lg font-semibold">{veh.id}</div>
                <div className="font-mono text-[11px] uppercase text-ink/60">
                  {label(veh.status)} · {veh.state === 'stale' ? 'last known ' : ''}
                  {ageText(vehAge)} ago · {veh.source}
                </div>
              </div>
            </div>
          ) : (
            <div>
              <div className="text-lg font-semibold text-ink/60">Not assigned yet</div>
              {nearby.length > 0 && (
                <ul aria-label="Nearest available ambulances" className="mt-1 font-mono text-[11px] uppercase text-ink/70">
                  {nearby.slice(0, 3).map((n) => (
                    <li key={n.id}>
                      {n.id} · {(n.distance_m / 1000).toFixed(1)} km · simulated
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
        <div className="bg-paper px-3 py-2">
          <div className="font-mono text-[11px] uppercase text-ink/60">Hospital</div>
          <div className="text-lg font-semibold">{hosp ? hosp.name : 'Being chosen'}</div>
        </div>
        <div className="bg-paper px-3 py-2">
          <div className="font-mono text-[11px] uppercase text-ink/60">Your location</div>
          <div className="font-mono text-sm">
            {you ? `${you[0].toFixed(5)}, ${you[1].toFixed(5)}` : 'unknown'}
            {t.location?.accuracy_m ? ` ±${Math.round(t.location.accuracy_m)} m` : ''}
          </div>
          <div className="font-mono text-[11px] uppercase text-ink/60">{approximate ? 'approximate' : fix ? `${ageText((now - fix.at) / 1000)} old` : t.location?.source ?? ''}</div>
        </div>
      </div>

      <div className="border-t-2 border-ink px-3 py-3">
        <div className="mb-1 font-mono text-[11px] uppercase text-ink/70">Automatic steps (no action needed)</div>
        {t.pipeline.length === 0 ? (
          <Empty title="Starting" />
        ) : (
          <ul className="grid gap-1 sm:grid-cols-2">
            {t.pipeline.map((p) => (
              <li key={p.step} className="flex items-baseline gap-2 text-sm">
                <Stamp tone={p.status === 'done' ? 'ok' : p.status === 'failed' || p.status === 'rejected' ? 'bad' : p.status === 'waiting' ? 'warn' : 'info'} className="!border-ink/50">
                  {p.status}
                </Stamp>
                <span className="font-semibold">{label(p.step)}</span>
                <span className="truncate text-ink/70">{p.detail}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
