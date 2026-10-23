import { useEffect, useMemo, useState } from 'react'
import { Circle, Marker, Polyline, Tooltip } from 'react-leaflet'
import MapView from '../../components/MapView'
import HeatLayer from '../../components/HeatLayer'
import { aedIcon, hospitalIcon, incidentIcon, sceneIcon, standbyIcon, vehicleIcon } from '../../components/markers'
import { Empty, ErrorBox, Loading, Stamp } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { useAuth } from '../../lib/auth'
import { useEvents } from '../../lib/events'
import { ageSince, ageText, label, PRIORITY_COLOR } from '../../lib/format'
import type { Aed, Decision, Forecast, Incident } from '../../lib/types'
import DispatchCompare from './DispatchCompare'
import FalseAlarmPanel, { LaneStamps } from './FalseAlarmPanel'
import { IncidentDetail, TimelineList } from './IncidentPanel'
import { latestDecision, useBoard } from './useBoard'

type Tab = 'incident' | 'dispatch' | 'timeline'
type Mode = 'none' | 'incident' | 'closure' | 'congestion'

const CATS = ['cardiac', 'stroke', 'trauma', 'neonatal', 'breathing', 'unconscious', 'other']

export default function CommandCenter() {
  const { user } = useAuth()
  const { board, refresh } = useBoard()
  const { link } = useEvents()
  const [sel, setSel] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('incident')
  const [layers, setLayers] = useState({ vehicles: true, hospitals: true, incidents: true, disruptions: true, aeds: false, demand: false })
  const [forecast, setForecast] = useState<Forecast | null>(null)
  const [forecastErr, setForecastErr] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('none')
  const [draft, setDraft] = useState<{ category: string; priority: string; count: string; point: [number, number] | null }>({ category: 'cardiac', priority: '1', count: '1', point: null })
  const [radius, setRadius] = useState(300)
  const [showClosed, setShowClosed] = useState(false)
  const [open, setOpen] = useState({ units: false, hospitals: false })
  const [aeds, setAeds] = useState<Aed[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [fitSel, setFitSel] = useState(0)
  const [confirmReset, setConfirmReset] = useState(false)

  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    if (layers.aeds && aeds.length === 0) {
      api.get<{ id: string; label: string; location: { lat: number; lon: number } }[]>('/api/aeds').then((r) =>
        setAeds(r.map((a) => ({ id: a.id, label: a.label, lat: a.location.lat, lon: a.location.lon, distance_m: 0, walk_low_s: 0, walk_s: 0, walk_high_s: 0 }))),
      ).catch(() => {})
    }
  }, [layers.aeds, aeds.length])
  useEffect(() => {
    if (!layers.demand) return
    let alive = true
    const load = () =>
      api.get<Forecast>('/api/demand/forecast').then((f) => { if (alive) { setForecast(f); setForecastErr(null) } }).catch((e) => alive && setForecastErr(e.message))
    load()
    const t = setInterval(load, 60000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [layers.demand])

  const open_ = useMemo(() => board.incidents.filter((i) => showClosed || !['closed', 'merged', 'cancelled_by_caller'].includes(i.status)), [board.incidents, showClosed])
  useEffect(() => {
    if (!sel && open_.length) setSel(open_[0].id)
  }, [open_, sel])

  const inc = board.incidents.find((i) => i.id === sel) ?? null
  const dec: Decision | null = inc ? latestDecision(board.decisions, inc.id) : null
  const faSel = inc ? board.falseAlarms.find((f) => f.incident_id === inc.id) : undefined
  const asg = inc ? board.assignments.filter((a) => a.incident_id === inc.id).slice(-1)[0] ?? null : null
  const hospName = (id: string) => board.hospitals.find((h) => h.id === id)?.name ?? id
  const now = Date.now() + tick * 0

  const incPos = (i: Incident | null): [number, number] | null => (i?.location_candidates[0] ? [i.location_candidates[0].lat, i.location_candidates[0].lon] : null)
  const selVeh = inc?.assigned_vehicle_id ? board.vehicles.find((v) => v.id === inc.assigned_vehicle_id) : null
  const selHosp = inc?.destination_id ? board.hospitals.find((h) => h.id === inc.destination_id) : null
  const fitPts: [number, number][] = [incPos(inc), selVeh?.location ? [selVeh.location.lat, selVeh.location.lon] : null, selHosp ? [selHosp.location.lat, selHosp.location.lon] : null].filter(Boolean) as [number, number][]

  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    setErr(null)
    try {
      await fn()
      await refresh()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const recommend = () => inc && run('recommend', async () => { await api.post('/api/dispatch/recommend', { incident_id: inc.id, autopilot: false }); setTab('dispatch') })
  const toggleAutopilot = () => run('autopilot', () => api.post('/api/dispatch/autopilot', { enabled: !board.autopilot?.enabled }))

  const onMapClick = (p: [number, number]) => {
    if (mode === 'incident') setDraft((d) => ({ ...d, point: p }))
    if (mode === 'closure' || mode === 'congestion') {
      void run('disruption', () =>
        api.post('/api/routing/disruptions', mode === 'closure'
          ? { kind: 'closure', lat: p[0], lon: p[1], radius_m: radius, note: 'closed from command center' }
          : { kind: 'congestion', lat: p[0], lon: p[1], radius_m: radius * 2, multiplier: 2.5, note: 'congestion from command center' }),
      )
      setMode('none')
    }
  }

  const createIncident = () =>
    draft.point &&
    run('create', async () => {
      const r = await api.post<Incident>('/api/incidents', {
        reported_category: draft.category,
        operator_priority: Number(draft.priority),
        patient_count: Number(draft.count),
        location: { lat: draft.point![0], lon: draft.point![1], accuracy_m: 10 },
        text: `Manual entry by ${user?.name ?? 'dispatcher'}`,
        channel: 'form',
      })
      setSel(r.id)
      setDraft((d) => ({ ...d, point: null }))
      setMode('none')
      setTab('incident')
    })

  const pri = (i: Incident) => (i.operator_priority?.value as number | undefined) ?? null
  const cursor = mode !== 'none' ? 'cursor-crosshair' : ''

  return (
    <div className="flex h-full min-h-[640px] flex-col bg-ink">
      {board.error && <div className="border-b border-amber bg-amber/10 px-3 py-1 font-mono text-xs uppercase text-amber">Server unreachable: showing last known data from {ageText(ageSince(board.loadedAt ? new Date(board.loadedAt).toISOString() : null, now))} ago. Nothing here is live until the link returns. {board.error}</div>}
      <div className="grid min-h-0 flex-1 lg:grid-cols-[340px_minmax(0,1fr)_430px]">
        {/* ---------------- left rail: queue ---------------- */}
        <aside aria-label="Incident queue" className="order-2 flex min-h-0 flex-col overflow-y-auto border-r hair bg-ink lg:order-1">
          <div className="border-b hair px-3 py-2">
            <div className="flex items-center justify-between">
              <h2 className="rail-title !text-base !text-bone">Incident queue</h2>
              <span className="font-mono text-xs text-rule">{open_.length} open</span>
            </div>
            <button
              onClick={toggleAutopilot}
              disabled={busy === 'autopilot' || !board.autopilot}
              aria-pressed={!!board.autopilot?.enabled}
              className={`mt-2 flex w-full items-center justify-between border px-2 py-1.5 text-left ${board.autopilot?.enabled ? 'border-amber bg-amber/10' : 'border-rule/40'}`}
              title="Demo Autopilot simulates a dispatcher approving the top recommendation. It is audited as demo-autopilot."
            >
              <span>
                <span className="block font-display text-lg font-bold uppercase leading-none">Demo Autopilot: {board.autopilot?.enabled ? 'on' : 'off'}</span>
                <span className="block text-[11px] text-rule">Simulated dispatcher approval after {board.autopilot?.delay_s ?? '?'}s. Audited.</span>
              </span>
              <Stamp tone={board.autopilot?.enabled ? 'warn' : 'mute'}>{board.autopilot?.enabled ? 'simulated' : 'manual'}</Stamp>
            </button>
          </div>

          {board.loading ? <Loading what="Loading board" /> : open_.length === 0 ? (
            <div className="p-3"><Empty title="No incidents" hint="Waiting for a caller, or add one with the tool below." /></div>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="w-1" aria-label="Priority" />
                  <th className="py-1 pl-2">Incident</th>
                  <th>Reason</th>
                  <th>Age</th>
                  <th className="pr-2 text-right">Unit</th>
                </tr>
              </thead>
              <tbody>
                {open_.map((i) => {
                  const p = pri(i)
                  const active = i.id === sel
                  return (
                    <tr key={i.id} onClick={() => { setSel(i.id); setFitSel((x) => x + 1) }} className={`cursor-pointer border-b hair ${active ? 'bg-bone/15' : 'hover:bg-bone/5'} ${i.status === 'pending' || i.status === 'cancelled_by_caller' ? 'opacity-60' : ''}`}>
                      <td className={`w-1 p-0 ${p ? PRIORITY_COLOR[p] : 'bg-rule/40'}`}><span className="sr-only">priority {p ?? 'unknown'}</span></td>
                      <td className="py-2 pl-2">
                        <button className="text-left" aria-pressed={active} aria-label={`Select incident ${i.id}`}>
                          <span className="block font-mono font-semibold">{i.id}</span>
                          <span className="block font-mono text-[10px] uppercase text-rule">P{p ?? '?'} · {label(i.status)}</span>
                          <LaneStamps inc={i} fa={board.falseAlarms.find((f) => f.incident_id === i.id)} />
                        </button>
                      </td>
                      <td className="max-w-[80px] truncate">
                        {i.reported_category ? label(String(i.reported_category.value)) : <span className="text-amber">unknown</span>}
                        {i.needs_review && <span className="ml-1 text-amber" title="needs review">!</span>}
                      </td>
                      <td className="font-mono text-xs text-rule">{ageText(ageSince(i.created_at, now))}</td>
                      <td className="pr-2 text-right font-mono text-xs">{i.assigned_vehicle_id ?? <span className="text-amber">none</span>}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
          <label className="flex items-center gap-2 border-b hair px-3 py-2 font-mono text-[11px] uppercase text-rule">
            <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} className="accent-orange" /> Show closed and cancelled
          </label>

          {/* tools */}
          <div className="border-b hair px-3 py-2">
            <h3 className="rail-title mb-2">Tools</h3>
            <div className="grid grid-cols-3 gap-1.5">
              <button className={mode === 'incident' ? 'btn-primary !px-2 !text-base' : 'btn-ghost !px-2 !text-base'} aria-pressed={mode === 'incident'} onClick={() => setMode(mode === 'incident' ? 'none' : 'incident')}>Add incident</button>
              <button className={mode === 'closure' ? 'btn-danger !bg-red !px-2 !text-base' : 'btn-danger !px-2 !text-base'} aria-pressed={mode === 'closure'} onClick={() => setMode(mode === 'closure' ? 'none' : 'closure')}>Close road</button>
              <button className={mode === 'congestion' ? 'btn-primary !px-2 !text-base' : 'btn-ghost !px-2 !text-base'} aria-pressed={mode === 'congestion'} onClick={() => setMode(mode === 'congestion' ? 'none' : 'congestion')}>Congestion</button>
            </div>
            {mode === 'incident' && (
              <div className="mt-2 space-y-2 border-l-2 border-orange pl-2">
                <div className="grid grid-cols-3 gap-1.5">
                  <label className="text-[10px] font-mono uppercase text-rule">Reason
                    <select className="mt-0.5 min-h-[36px] w-full border border-rule/60 bg-ink px-1 text-sm normal-case" value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value })}>{CATS.map((c) => <option key={c}>{c}</option>)}</select>
                  </label>
                  <label className="text-[10px] font-mono uppercase text-rule">Priority
                    <select className="mt-0.5 min-h-[36px] w-full border border-rule/60 bg-ink px-1 text-sm" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })}>{[1, 2, 3, 4].map((p) => <option key={p}>{p}</option>)}</select>
                  </label>
                  <label className="text-[10px] font-mono uppercase text-rule">Patients
                    <input className="mt-0.5 min-h-[36px] w-full border border-rule/60 bg-ink px-1 text-sm" type="number" min={1} max={500} value={draft.count} onChange={(e) => setDraft({ ...draft, count: e.target.value })} />
                  </label>
                </div>
                <p className="text-xs text-rule">{draft.point ? `Placed at ${draft.point[0].toFixed(4)}, ${draft.point[1].toFixed(4)}` : 'Click the map to place the incident.'}</p>
                <button className="btn-primary w-full" disabled={!draft.point || busy === 'create'} onClick={createIncident}>{busy === 'create' ? 'Creating…' : 'Create incident'}</button>
              </div>
            )}
            {(mode === 'closure' || mode === 'congestion') && (
              <div className="mt-2 border-l-2 border-red pl-2">
                <label className="font-mono text-[10px] uppercase text-rule">Radius
                  <select className="ml-2 min-h-[32px] border border-rule/60 bg-ink px-1 text-sm" value={radius} onChange={(e) => setRadius(Number(e.target.value))}>{[150, 300, 600, 1000].map((r) => <option key={r} value={r}>{r} m</option>)}</select>
                </label>
                <p className="mt-1 text-xs text-rule">Click the map where the {mode === 'closure' ? 'road is closed' : 'traffic builds up'}. Drivers on an affected route get a one-tap reroute prompt.</p>
              </div>
            )}
            {board.disruptions.length > 0 && (
              <ul className="mt-2">
                {board.disruptions.map((d) => (
                  <li key={d.id} className="flex items-center justify-between gap-2 border-b hair py-1 text-xs">
                    <span><Stamp tone={d.kind === 'closure' ? 'bad' : 'warn'}>{d.kind}</Stamp> <span className="font-mono">{d.id}</span> {d.radius_m ? `${d.radius_m} m` : ''}{d.multiplier > 1 ? ` ×${d.multiplier}` : ''}</span>
                    <button className="font-mono text-[11px] uppercase text-orange underline" onClick={() => run('rm', () => api.del(`/api/routing/disruptions/${d.id}`))}>Remove</button>
                  </li>
                ))}
              </ul>
            )}
            {user?.role === 'admin' && (
              <div className="mt-3 border-t hair pt-2">
                {!confirmReset ? (
                  <button className="btn-ghost w-full !min-h-[36px] !text-base" onClick={() => setConfirmReset(true)}>Reset demo data</button>
                ) : (
                  <div className="border border-red p-2">
                    <p className="text-xs">This wipes every incident and the audit log, then reseeds 20 units and 12 hospitals.</p>
                    <div className="mt-2 grid grid-cols-2 gap-1.5">
                      <button className="btn-danger !min-h-[36px] !bg-red !text-base" disabled={busy === 'reset'} onClick={() => { void run('reset', () => api.post('/api/admin/reset-demo')); setConfirmReset(false); setSel(null) }}>Confirm reset</button>
                      <button className="btn-ghost !min-h-[36px] !text-base" onClick={() => setConfirmReset(false)}>Cancel</button>
                    </div>
                  </div>
                )}
              </div>
            )}
            {err && <div className="mt-2"><ErrorBox message={err} /></div>}
          </div>

          {layers.demand && (
            <div className="border-b hair px-3 py-2" aria-label="Pre-positioning">
              <div className="flex items-center justify-between"><h3 className="rail-title">Pre-positioning</h3><Stamp tone="warn">prototype approximation</Stamp></div>
              {forecastErr && <ErrorBox message={forecastErr} />}
              {!forecast && !forecastErr ? <Loading what="Forecasting" /> : forecast && (
                <>
                  <p className="mt-1 font-mono text-[11px] uppercase text-rule">{forecast.day_type} · {forecast.bucket} · synthetic history of {forecast.history_count} incidents</p>
                  <p className="mt-1 text-sm">Expected demand <b className="font-mono">{forecast.expected_per_hour_total.toFixed(2)}</b> per hour.</p>
                  <div className="mt-2 grid grid-cols-2 gap-2 font-mono">
                    <div className="border-l-2 border-rule/50 pl-2"><div className="text-[10px] uppercase text-rule">Coverage now</div><div className="text-2xl font-semibold">{(forecast.coverage_before * 100).toFixed(0)}%</div></div>
                    <div className="border-l-2 border-green pl-2"><div className="text-[10px] uppercase text-rule">After standby moves</div><div className="text-2xl font-semibold text-green">{(forecast.coverage_after * 100).toFixed(0)}%</div></div>
                  </div>
                  {forecast.moves.length > 0 ? (
                    <table className="mt-2 w-full text-xs">
                      <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Move</th><th>To</th><th className="text-right">Distance</th></tr></thead>
                      <tbody>{forecast.moves.map((m) => <tr key={m.standby} className="border-b hair"><td className="py-1 font-mono font-semibold">{m.vehicle_id}</td><td className="font-mono">{m.standby}</td><td className="text-right font-mono">{m.distance_km.toFixed(1)} km</td></tr>)}</tbody>
                    </table>
                  ) : <p className="mt-2 text-xs text-rule">No available unit to move.</p>}
                  <details className="mt-2"><summary className="cursor-pointer font-mono text-[10px] uppercase text-rule">How this is computed</summary><p className="mt-1 text-xs text-bone/80">{forecast.approximation}</p><p className="mt-1 font-mono text-[10px] text-rule">{forecast.explain.formula}</p></details>
                  <p className="mt-1 text-[11px] text-rule">Suggestions only. A dispatcher decides whether to move a unit.</p>
                </>
              )}
            </div>
          )}
          <button className="flex items-center justify-between border-b hair px-3 py-2 text-left" aria-expanded={open.units} onClick={() => setOpen({ ...open, units: !open.units })}>
            <span className="rail-title">Units ({board.vehicles.length})</span><span className="font-mono text-xs">{open.units ? '−' : '+'}</span>
          </button>
          {open.units && (
            <table className="w-full text-xs">
              <tbody>
                {board.vehicles.map((v) => (
                  <tr key={v.id} className="border-b hair">
                    <td className={`w-1 p-0 ${v.location_state === 'live' ? (v.status === 'available' ? 'bg-green' : 'bg-orange') : 'bg-rule/60'}`} />
                    <td className="py-1 pl-2 font-mono font-semibold">{v.id}</td>
                    <td className="text-rule">{v.capability_tags.join(' ')}</td>
                    <td>{label(v.status)}</td>
                    <td className="pr-2 text-right font-mono">
                      {v.location_state === 'live' ? ageText(v.location_age_s) : <Stamp tone="warn" title="Last known position, not live">stale {ageText(v.location_age_s)}</Stamp>}
                      {v.availability === 'unknown' && <Stamp tone="warn" className="ml-1">unknown</Stamp>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <button className="flex items-center justify-between border-b hair px-3 py-2 text-left" aria-expanded={open.hospitals} onClick={() => setOpen({ ...open, hospitals: !open.hospitals })}>
            <span className="rail-title">Hospitals ({board.hospitals.length})</span><span className="font-mono text-xs">{open.hospitals ? '−' : '+'}</span>
          </button>
          {open.hospitals && (
            <table className="w-full text-xs">
              <tbody>
                {board.hospitals.map((h) => {
                  const stale = Object.values(h.resources).some((r) => r.stale && r.reported_status === 'available')
                  const unk = Object.values(h.resources).some((r) => r.status === 'unknown')
                  return (
                    <tr key={h.id} className="border-b hair">
                      <td className={`w-1 p-0 ${stale ? 'bg-amber' : 'bg-green'}`} />
                      <td className="py-1 pl-2 font-mono">{h.id}</td>
                      <td className="text-rule">{h.capabilities.filter((c) => c !== 'emergency').join(' ') || 'emergency'}</td>
                      <td className="pr-2 text-right">{stale ? <Stamp tone="warn">stale data</Stamp> : unk ? <Stamp tone="mute">some unknown</Stamp> : <Stamp tone="ok">fresh</Stamp>}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </aside>

        {/* ---------------- map ---------------- */}
        <div className={`relative order-1 h-[52vh] min-h-[360px] lg:order-2 lg:h-auto ${cursor}`}>
          <MapView
            className="absolute inset-0"
            zoom={12}
            onClick={onMapClick}
            fit={fitPts.length ? fitPts : undefined}
            fitKey={`${sel}-${fitSel}-${fitPts.length}`}
            label="Operations map"
          >
            {layers.disruptions && board.disruptions.map((d) => d.lat != null && d.lon != null && d.radius_m ? (
              <Circle key={d.id} center={[d.lat, d.lon]} radius={d.radius_m} pathOptions={{ color: d.kind === 'closure' ? '#C0182B' : '#E9A91A', weight: 3, dashArray: '8 6', fillOpacity: 0.18 }}>
                <Tooltip>{d.kind} {d.id}{d.multiplier > 1 ? ` ×${d.multiplier}` : ''}</Tooltip>
              </Circle>
            ) : null)}
            {dec?.scene_route && dec.status !== 'superseded' && <Polyline positions={dec.scene_route.polyline} pathOptions={{ color: '#E4570F', weight: 5, dashArray: '10 8' }} />}
            {dec?.hospital_route && dec.status !== 'superseded' && <Polyline positions={dec.hospital_route.polyline} pathOptions={{ color: '#15120E', weight: 4, dashArray: '2 8' }} />}
            {layers.demand && forecast && <HeatLayer points={forecast.heat} />}
            {layers.demand && forecast?.standby.map((s) => (
              <Marker key={s.id} position={[s.lat, s.lon]} icon={standbyIcon(s.id)}>
                <Tooltip direction="top">Standby {s.id}: covers {(s.covers_share * 100).toFixed(1)}% of forecast demand</Tooltip>
              </Marker>
            ))}
            {layers.aeds && aeds.map((a) => <Marker key={a.id} position={[a.lat, a.lon]} icon={aedIcon(false)}><Tooltip>{a.label}</Tooltip></Marker>)}
            {layers.hospitals && board.hospitals.map((h) => {
              const stale = Object.values(h.resources).some((r) => r.stale && r.reported_status === 'available')
              return (
                <Marker key={h.id} position={[h.location.lat, h.location.lon]} icon={hospitalIcon({ stale })}>
                  <Tooltip direction="top">{h.name} · {h.capabilities.filter((c) => c !== 'emergency').join(', ') || 'emergency'}{stale ? ' · STALE DATA' : ''}</Tooltip>
                </Marker>
              )
            })}
            {layers.vehicles && board.vehicles.filter((v) => v.location).map((v) => (
              <Marker key={v.id} position={[v.location!.lat, v.location!.lon]} icon={vehicleIcon({ id: v.id, status: v.status, stale: v.location_state !== 'live', selected: v.id === inc?.assigned_vehicle_id, simulated: v.location?.source === 'simulated' && v.status !== 'available' })} zIndexOffset={v.id === inc?.assigned_vehicle_id ? 800 : 0}>
                <Tooltip direction="top">
                  {v.id} · {v.capability_tags.join('+')} · {label(v.status)} · {v.location_state === 'live' ? `updated ${ageText(v.location_age_s)} ago` : `LAST KNOWN, ${ageText(v.location_age_s)} old (not live)`}
                </Tooltip>
              </Marker>
            ))}
            {layers.incidents && open_.map((i) => {
              const p = incPos(i)
              return p ? (
                <Marker key={i.id} position={p} icon={i.id === sel ? sceneIcon() : incidentIcon({ priority: pri(i), n: String(pri(i) ?? '?') })} eventHandlers={{ click: () => setSel(i.id) }} zIndexOffset={900}>
                  <Tooltip direction="top">{i.id} · {i.reported_category ? String(i.reported_category.value) : 'reason unknown'}</Tooltip>
                </Marker>
              ) : null
            })}
            {draft.point && mode === 'incident' && <Marker position={draft.point} icon={incidentIcon({ priority: Number(draft.priority), n: '+' })} />}
          </MapView>

          {/* docked layer bar (not a floating card) */}
          <div className="absolute inset-x-0 top-0 z-[1000] flex flex-wrap items-center gap-px border-b hair bg-ink/95 px-2 py-1">
            <span className="mr-2 font-mono text-[10px] uppercase text-rule">Layers</span>
            {(['vehicles', 'hospitals', 'incidents', 'disruptions', 'aeds', 'demand'] as const).map((k) => (
              <button key={k} aria-pressed={layers[k]} onClick={() => setLayers({ ...layers, [k]: !layers[k] })} title={k === 'demand' ? 'Demand forecast heat layer and standby points. Prototype approximation on synthetic history.' : undefined} className={`min-h-[28px] border px-2 font-mono text-[11px] uppercase ${layers[k] ? 'border-bone bg-bone text-ink' : 'border-rule/50 text-rule'}`}>{k === 'aeds' ? 'AEDs' : k === 'demand' ? 'Demand' : k}</button>
            ))}
            <span className="ml-auto flex items-center gap-2 font-mono text-[10px] uppercase text-rule">
              {mode !== 'none' && <Stamp tone="info">click the map to {mode === 'incident' ? 'place incident' : mode === 'closure' ? 'close a road' : 'add congestion'}</Stamp>}
              <Stamp tone="mute">mock routing</Stamp>
              <Stamp tone={link === 'live' ? 'ok' : 'warn'}>{link}</Stamp>
            </span>
          </div>
        </div>

        {/* ---------------- right rail: selected incident ---------------- */}
        <aside aria-label="Selected incident" className="order-3 flex min-h-0 flex-col overflow-y-auto border-l hair bg-ink">
          {!inc ? (
            <div className="p-3"><Empty title="No incident selected" hint="Pick one from the queue or click a diamond on the map." /></div>
          ) : (
            <>
              <div className="flex items-stretch border-b hair">
                <span className={`w-1.5 ${pri(inc) ? PRIORITY_COLOR[pri(inc)!] : 'bg-rule/40'}`} aria-hidden="true" />
                <div className="flex-1 px-3 py-2">
                  <div className="flex items-baseline gap-2">
                    <h2 className="font-display text-3xl font-bold leading-none">{inc.id}</h2>
                    <Stamp tone={inc.status === 'closed' ? 'mute' : inc.status === 'new' ? 'warn' : 'info'}>{inc.status}</Stamp>
                    <Stamp tone="mute">{inc.channel === 'sos' ? 'sos' : 'manual'}</Stamp>
                  </div>
                  <div className="mt-0.5 text-sm">
                    {inc.reported_category ? label(String(inc.reported_category.value)) : <span className="text-amber">Reason unknown (unknown is not minor)</span>} · {ageText(ageSince(inc.created_at, now))} old
                    {inc.assigned_vehicle_id && <> · unit <span className="font-mono">{inc.assigned_vehicle_id}</span> to {inc.destination_id ? hospName(inc.destination_id) : '?'}</>}
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-[1fr_auto] border-b hair">
                <div role="tablist" className="flex">
                  {(['incident', 'dispatch', 'timeline'] as Tab[]).map((t) => (
                    <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)} className={`label min-h-[40px] flex-1 border-r hair text-lg ${tab === t ? 'bg-bone text-ink' : 'hover:bg-bone/10'}`}>{t === 'dispatch' ? 'Dispatch compare' : t}</button>
                  ))}
                </div>
                <button className="btn-primary !min-h-[40px] !px-3 !text-base" onClick={recommend} disabled={busy === 'recommend' || ['closed', 'merged', 'pending', 'cancelled_by_caller'].includes(inc.status)} title="Rank unit and destination pairs. Nothing is committed until approved.">
                  {busy === 'recommend' ? 'Ranking…' : 'Recommend'}
                </button>
              </div>
              {tab === 'incident' && (
                <>
                  {faSel && <FalseAlarmPanel inc={inc} fa={faSel} onChanged={refresh} />}
                  <IncidentDetail inc={inc} onChanged={refresh} />
                </>
              )}
              {tab === 'dispatch' && (dec ? (
                <DispatchCompare decision={dec} assignment={asg} onChanged={refresh} onRecommend={recommend} hospitalName={hospName} busyRecommend={busy === 'recommend'} />
              ) : (
                <div className="p-3"><Empty title="No recommendation yet" hint="Press Recommend to rank units and hospitals." /></div>
              ))}
              {tab === 'timeline' && <TimelineList incidentId={inc.id} />}
            </>
          )}
        </aside>
      </div>

      {/* ---------------- alerts strip ---------------- */}
      <div role="region" aria-label="Alerts" className="flex h-[74px] shrink-0 items-stretch border-t-2 border-rule/50 bg-ink">
        <div className="flex w-[110px] shrink-0 flex-col justify-center border-r hair px-3">
          <span className="rail-title">Alerts</span>
          <span className={`font-display text-3xl font-bold leading-none ${board.alerts.some((a) => a.severity === 'red') ? 'text-red' : board.alerts.length ? 'text-amber' : 'text-green'}`}>{board.alerts.length}</span>
        </div>
        {board.alerts.length === 0 ? (
          <div className="flex flex-1 items-center px-3 text-sm text-rule">No alerts. Every unit is reporting and every assignment is answered.</div>
        ) : (
          <ul className="flex flex-1 overflow-x-auto">
            {board.alerts.map((a) => (
              <li key={a.id} className="shrink-0">
                <button onClick={() => a.incident_id && setSel(a.incident_id)} className={`flex h-full w-[300px] items-stretch border-r hair text-left ${a.pulse ? (a.severity === 'red' ? 'alert-pulse' : 'amber-pulse') : ''}`}>
                  <span className={`w-1 shrink-0 ${a.severity === 'red' ? 'bg-red' : 'bg-amber'}`} aria-hidden="true" />
                  <span className="px-2 py-1">
                    <span className="flex items-center gap-1.5"><Stamp tone={a.severity === 'red' ? 'bad' : 'warn'}>{a.severity === 'red' ? 'urgent' : 'check'}</Stamp><span className="font-mono text-[10px] uppercase text-rule">{label(a.kind)}</span></span>
                    <span className="line-clamp-2 block text-xs leading-tight">{a.text}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
