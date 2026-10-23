import { useCallback, useEffect, useRef, useState } from 'react'
import { Marker, Polyline, Tooltip } from 'react-leaflet'
import { Ambulance, HazardBand } from '../../components/art'
import MapView from '../../components/MapView'
import { hospitalIcon, sceneIcon, signalIcon, vehicleIcon } from '../../components/markers'
import { ErrorBox, Loading, Stamp } from '../../components/ui'
import { api, ApiError, uid } from '../../lib/api'
import { useAuth } from '../../lib/auth'
import { useEvents, useLiveRefresh } from '../../lib/events'
import { ageText, dist, etaRange, label } from '../../lib/format'
import { useWatchPosition } from '../../lib/geo'
import { speak, speechSynthesisSupported, stopSpeaking } from '../../lib/speech'
import type { Assignment, NavPlan, VehicleView } from '../../lib/types'

interface Me {
  vehicle: VehicleView
  assignment: Assignment | null
  incident: {
    id: string
    status: string
    category: string | null
    category_source: string | null
    priority: number | null
    patient_count: number | null
    unknowns: string[]
    arrived: boolean
    handover_status: string | null
    location: { lat: number; lon: number; accuracy_m: number | null; approximate: boolean } | null
    hospital: { id: string; name: string; lat: number; lon: number } | null
  } | null
  plan: NavPlan | null
  recalled?: { incident_id: string; reason: string | null; at: string } | null
}

const FLOW: { status: string; label: string; need: string[] }[] = [
  { status: 'en_route', label: 'En route', need: ['accepted'] },
  { status: 'on_scene', label: 'On scene', need: ['en_route'] },
  { status: 'transporting', label: 'Transporting', need: ['on_scene'] },
  { status: 'arrived', label: 'Arrived', need: ['transporting'] },
]
const ORDER = ['assigned', 'accepted', 'en_route', 'on_scene', 'transporting', 'arrived']

function Arrow({ maneuver }: { maneuver?: string }) {
  const m = maneuver ?? 'continue'
  const rot = m.includes('left') ? -90 : m.includes('right') ? 90 : m === 'uturn' ? 180 : 0
  return (
    <svg viewBox="0 0 64 64" className="h-20 w-20 shrink-0" aria-hidden="true" style={{ transform: `rotate(${rot}deg)` }}>
      {m === 'arrive' ? (
        <path d="M32 6c-10 0-18 8-18 18 0 14 18 34 18 34s18-20 18-34c0-10-8-18-18-18z" fill="#E4570F" stroke="#ECE6D8" strokeWidth="3" />
      ) : (
        <path d="M32 4 52 30H40v30H24V30H12z" fill="#E4570F" stroke="#ECE6D8" strokeWidth="3" strokeLinejoin="miter" />
      )}
    </svg>
  )
}

export default function DriverApp() {
  const { user } = useAuth()
  const { link } = useEvents()
  const vid = user?.vehicle_id ?? ''
  const [me, setMe] = useState<Me | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [voice, setVoice] = useState(false)
  const [gpsMode, setGpsMode] = useState<'sim' | 'device'>('sim')
  const [simRunning, setSimRunning] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const gps = useWatchPosition()
  const acked = useRef<string | null>(null)
  const spoken = useRef<{ key: string; stage: number }>({ key: '', stage: -1 })
  const lastPost = useRef(0)

  const load = useCallback(async () => {
    try {
      setMe(await api.get<Me>('/api/driver/me'))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])
  useLiveRefresh(load, { pollMs: 2500, filter: (e) => e.event_type !== 'vehicle.location_updated' || e.entity_id === vid })

  const v = me?.vehicle
  const inc = me?.incident
  const asg = me?.assignment
  const plan = me?.plan
  const status = v?.status ?? 'available'
  const hasJob = !!inc && !!asg && asg.state !== 'rejected' && inc.status !== 'closed'

  // being shown the job means the crew has seen it: delivered, then acknowledged. Accepting is separate.
  useEffect(() => {
    if (!asg || asg.state === 'rejected' || acked.current === asg.id) return
    acked.current = asg.id
    void (async () => {
      try {
        await api.post(`/api/assignments/${asg.id}/delivered`)
        await api.post(`/api/assignments/${asg.id}/acknowledge`)
        if (voice) speak(`New assignment. ${inc?.category ? label(inc.category) : 'Reason unknown'}.`)
        void load()
      } catch {
        /* the watchdog will show it as unacknowledged: never pretend */
      }
    })()
  }, [asg, inc, voice, load])

  // device GPS: send real positions (labelled measured) while a job is active
  useEffect(() => {
    if (gpsMode !== 'device' || !hasJob) return
    gps.start()
    return () => gps.stop()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gpsMode, hasJob])
  useEffect(() => {
    const f = gps.fix
    if (gpsMode !== 'device' || !f || !vid || Date.now() - lastPost.current < 3000) return
    lastPost.current = Date.now()
    api.patch(`/api/vehicles/${vid}/state`, {
      update_id: uid(),
      occurred_at: new Date().toISOString(),
      location: { lat: f.lat, lon: f.lon, accuracy_m: f.accuracy_m },
      source: 'measured',
    }).catch(() => {})
  }, [gps.fix, gpsMode, vid])

  // voice prompts at 500 m, 120 m and at the turn
  useEffect(() => {
    if (!voice || !plan?.next_step || plan.distance_to_next_m == null) return
    const key = `${plan.phase}-${plan.next_step.lat}-${plan.next_step.lon}`
    const d = plan.distance_to_next_m
    const stage = d <= 30 ? 2 : d <= 120 ? 1 : d <= 500 ? 0 : -1
    if (stage < 0) return
    if (spoken.current.key === key && spoken.current.stage >= stage) return
    spoken.current = { key, stage }
    const ins = plan.next_step.instruction
    speak(stage === 0 ? `In ${Math.round(d / 50) * 50} metres, ${ins}` : stage === 1 ? `${ins} ahead` : `${ins} now`, { cancel: true })
  }, [plan, voice])
  useEffect(() => {
    if (voice && plan?.reroute) speak(`Reroute available. ${plan.reroute.message}`)
  }, [plan?.reroute?.id, voice]) // eslint-disable-line react-hooks/exhaustive-deps
  // stable-route policy: say what changed once, and say when the unit is off route
  useEffect(() => {
    if (voice && plan?.route_notice && !plan.route_notice.acknowledged) speak(plan.route_notice.message, { cancel: true })
  }, [plan?.route_notice?.id, voice]) // eslint-disable-line react-hooks/exhaustive-deps
  const offState = plan?.off_route?.state
  useEffect(() => {
    if (voice && offState === 'off_route') speak('Off route. Finding the quickest way back.', { cancel: true })
  }, [offState, voice]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => stopSpeaking(), [])

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    setActionErr(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const accept = () => asg && act('accept', () => api.post(`/api/assignments/${asg.id}/accept`))
  const reject = () => asg && act('reject', async () => { await api.post(`/api/assignments/${asg.id}/reject`, { note: reason || 'Rejected by crew' }); setRejecting(false); setReason('') })
  const setStatus = (s: string) =>
    act(s, async () => {
      if (s === 'arrived') {
        await api.post(`/api/driver/${vid}/arrived`)
        return
      }
      await api.patch(`/api/vehicles/${vid}/state`, { update_id: uid(), occurred_at: new Date().toISOString(), status: s, source: 'reported' })
    })
  const sim = () =>
    act('sim', async () => {
      if (simRunning) {
        await api.post(`/api/driver/${vid}/stop-movement`)
        setSimRunning(false)
      } else {
        await api.post(`/api/driver/${vid}/simulate-movement`)
        setSimRunning(true)
      }
    })
  const reroute = (id: string, accept: boolean) => act('reroute', () => api.post(`/api/driver/${vid}/reroute/${id}/${accept ? 'accept' : 'decline'}`))
  const ackNotice = (id: string) => act('ack', () => api.post(`/api/driver/${vid}/route-notice/${id}/ack`))
  const wrongTurn = () => act('wrong', () => api.post(`/api/driver/${vid}/simulate-wrong-turn?offset_m=220`))

  useEffect(() => {
    if (plan && plan.remaining_m < 3) setSimRunning(false)
  }, [plan])

  if (!me && !err) return <Loading what="Loading unit" />
  const pos: [number, number] | null = v?.location ? [v.location.lat, v.location.lon] : null
  const pending = asg && ['sent', 'delivered_if_known', 'acknowledged', 'timed_out'].includes(asg.state) && status === 'assigned'
  const idx = ORDER.indexOf(status)
  const arrived = !!inc?.arrived

  return (
    <div className="mx-auto flex min-h-full max-w-6xl flex-col bg-ink">
      <HazardBand height={10} dark />
      <header className="flex flex-wrap items-center gap-3 border-b hair px-3 py-2">
        <div className="font-display text-5xl font-bold leading-none">{vid}</div>
        <Stamp tone={status === 'available' ? 'ok' : 'info'}>{label(status)}</Stamp>
        <Stamp tone={v?.location_state === 'live' ? 'ok' : 'warn'} title="Position freshness">
          GPS {v?.location_state === 'live' ? `live ${ageText(v.location_age_s)}` : `stale ${ageText(v?.location_age_s)}`}
        </Stamp>
        <Stamp tone="mute">{v?.location?.source === 'simulated' ? 'simulated gps' : v?.location?.source === 'measured' ? 'device gps' : 'gps unknown'}</Stamp>
        <Stamp tone={link === 'live' ? 'ok' : 'warn'}>{link === 'live' ? 'online' : link}</Stamp>
        {speechSynthesisSupported() && (
          <button className={`ml-auto min-h-[56px] border px-4 font-mono text-sm uppercase ${voice ? 'border-orange bg-orange text-ink' : 'border-rule/60'}`} aria-pressed={voice} onClick={() => { setVoice(!voice); if (!voice) speak('Voice prompts on'); else stopSpeaking() }}>
            Voice {voice ? 'on' : 'off'}
          </button>
        )}
      </header>
      {err && <div className="p-3"><ErrorBox message={err} onRetry={load} /></div>}

      {!hasJob ? (
        <div className="grid flex-1 place-items-center px-4 py-10">
          <div className="max-w-md text-center">
            <Ambulance className="mx-auto h-28 w-auto" />
            <h1 className="mt-4 font-display text-5xl font-bold uppercase leading-none">Standing by</h1>
            <p className="mt-2 text-lg text-bone/80">No assignment. This screen switches to the job the moment one is sent to {vid}.</p>
            <p className="mt-3 font-mono text-xs uppercase text-rule">
              Availability: {v?.availability ?? 'unknown'} · last position {ageText(v?.location_age_s)} ago · {v?.capability_tags.join(' + ')}
            </p>
            {inc?.status === 'closed' && <p className="mt-3 border-l-4 border-green bg-green/10 px-3 py-2 text-left text-sm">Last case closed after hospital handover.</p>}
            {me?.recalled && (
              <p role="status" className="mt-3 border-l-4 border-amber bg-amber/10 px-3 py-2 text-left text-sm">
                <b>Assignment recalled</b> for {me.recalled.incident_id}: {me.recalled.reason ?? 'the caller reported they are safe'}. You are available again.
              </p>
            )}
          </div>
        </div>
      ) : pending ? (
        <div className="grid flex-1 content-start gap-4 px-3 py-4 lg:grid-cols-2">
          <section aria-label="New assignment" className="border-2 border-orange">
            <div className="bg-orange px-3 py-1.5 font-display text-2xl font-bold uppercase text-ink">New assignment · {inc!.id}</div>
            <dl className="divide-y hair px-3">
              <div className="flex justify-between py-2"><dt className="font-mono text-xs uppercase text-rule">Reason</dt><dd className="text-xl font-semibold">{inc!.category ? label(inc!.category) : 'Not reported yet'} {inc!.category_source && <Stamp tone="mute" className="ml-1">{label(inc!.category_source)}</Stamp>}</dd></div>
              <div className="flex justify-between py-2"><dt className="font-mono text-xs uppercase text-rule">Priority</dt><dd className="font-display text-3xl font-bold leading-none">{inc!.priority ? `P${inc!.priority}` : 'unknown'}</dd></div>
              <div className="flex justify-between py-2"><dt className="font-mono text-xs uppercase text-rule">Patients</dt><dd className="text-xl font-semibold">{inc!.patient_count ?? 'unknown'}</dd></div>
              <div className="flex justify-between py-2"><dt className="font-mono text-xs uppercase text-rule">Destination</dt><dd className="text-xl font-semibold">{inc!.hospital?.name ?? 'to be set'}</dd></div>
              <div className="flex justify-between py-2"><dt className="font-mono text-xs uppercase text-rule">To scene</dt><dd className="font-mono text-xl">{plan ? `${etaRange(plan.remaining_low_s, plan.remaining_high_s)} · ${dist(plan.remaining_m)}` : '…'}</dd></div>
              {inc!.unknowns.length > 0 && <div className="py-2"><div className="font-mono text-xs uppercase text-rule">Still unknown</div><div className="mt-1 flex flex-wrap gap-1">{inc!.unknowns.map((u) => <Stamp key={u} tone="warn">? {label(u)}</Stamp>)}</div></div>}
            </dl>
            <div className="px-3 pb-2 font-mono text-[11px] uppercase text-rule">Seen by crew: {asg!.state === 'sent' ? 'sending…' : 'yes'} · accepted: no. Opening this job is not accepting it.</div>
          </section>
          <section aria-label="Respond" className="space-y-3">
            {asg!.state === 'timed_out' && <p className="border-l-4 border-red bg-red/10 px-3 py-2 text-sm">The dispatcher has been alerted that this job went unanswered. You can still accept it.</p>}
            <button className="btn-go btn-touch w-full !min-h-[72px] !text-3xl" onClick={accept} disabled={busy === 'accept'}>{busy === 'accept' ? 'Accepting…' : 'Accept job'}</button>
            {!rejecting ? (
              <button className="btn-danger btn-touch w-full" onClick={() => setRejecting(true)}>Reject</button>
            ) : (
              <div className="border-2 border-red p-3">
                <label htmlFor="rej" className="rail-title block">Reason for rejecting</label>
                <input id="rej" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. vehicle fault" className="mt-1 min-h-[56px] w-full border border-rule/60 bg-ink px-3 text-lg" />
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <button className="btn-danger btn-touch !bg-red" onClick={reject} disabled={busy === 'reject'}>Confirm reject</button>
                  <button className="btn-ghost btn-touch" onClick={() => setRejecting(false)}>Back</button>
                </div>
              </div>
            )}
            {actionErr && <ErrorBox message={actionErr} />}
          </section>
        </div>
      ) : (
        <div className="grid flex-1 lg:grid-cols-[1.25fr_1fr]">
          <div className="relative h-[44vh] min-h-[300px] border-b hair lg:h-auto lg:border-b-0 lg:border-r">
            <MapView
              className="absolute inset-0"
              center={pos ?? [30.901, 75.857]}
              zoom={15}
              fit={[pos, plan ? [plan.target.lat, plan.target.lon] : null].filter(Boolean) as [number, number][]}
              fitKey={`${plan?.phase}-${Math.floor((plan?.along_m ?? 0) / 500)}-${plan?.reroute?.id ?? ''}`}
              label="Navigation map"
            >
              {(plan?.backups ?? []).map((b) => (
                <Polyline key={b.id} positions={b.polyline} pathOptions={{ color: '#BDB5A3', weight: 3, opacity: 0.55, dashArray: '2 8' }} />
              ))}
              {plan && <Polyline key={`route-v${plan.route_lock?.version ?? 1}`} positions={plan.route.polyline} pathOptions={{ color: '#E4570F', weight: 6 }} />}
              {plan?.reroute && <Polyline positions={plan.reroute.route?.polyline ?? []} pathOptions={{ color: '#3F7A3A', weight: 5, dashArray: '8 8' }} />}
              {plan?.green_corridor.signals.map((s) => (
                <Marker key={s.id} position={[s.lat, s.lon]} icon={signalIcon(s.along_m < plan.along_m + 500)} interactive={false} />
              ))}
              {plan && plan.target.kind === 'scene' && <Marker position={[plan.target.lat, plan.target.lon]} icon={sceneIcon()}><Tooltip permanent direction="top">{plan.target.name}</Tooltip></Marker>}
              {plan && plan.target.kind === 'hospital' && <Marker position={[plan.target.lat, plan.target.lon]} icon={hospitalIcon({ stale: false })}><Tooltip permanent direction="top">{plan.target.name}</Tooltip></Marker>}
              {pos && v && <Marker position={pos} icon={vehicleIcon({ id: v.id, status, stale: v.location_state !== 'live', simulated: v.location?.source === 'simulated' })} zIndexOffset={1000} />}
            </MapView>
          </div>

          <div className="space-y-0 overflow-y-auto">
            {plan?.route_lock && (
              <div className="flex flex-wrap items-center gap-2 border-b hair px-3 py-2" aria-label="Route status">
                <Stamp tone="ok" title="The route is committed. It only changes when the policy says it is clearly worth it, or the road is blocked.">route locked</Stamp>
                <span className="font-mono text-xs uppercase text-rule">changes {plan.route_lock.reroutes} · v{plan.route_lock.version} · {plan.route_lock.policy_version}</span>
              </div>
            )}
            {plan?.off_route && (
              <section aria-label="Off route" role="alert" className={`border-b-2 px-3 py-3 ${plan.off_route.state === 'off_route' ? 'amber-pulse border-red bg-red/10' : 'border-amber bg-amber/10'}`}>
                <div className="flex items-center gap-2">
                  <Stamp tone={plan.off_route.state === 'off_route' ? 'bad' : 'warn'}>{plan.off_route.state === 'uncertain' ? 'gps uncertain' : plan.off_route.state === 'suspect' ? 'checking position' : 'off route'}</Stamp>
                  <span className="font-mono text-[11px] uppercase text-rule">{Math.round(plan.off_route.deviation_m)} m from the route</span>
                </div>
                <p className="mt-1 text-lg font-semibold leading-snug">{plan.off_route.message}</p>
              </section>
            )}
            {plan?.route_notice && !plan.route_notice.acknowledged && (
              <section aria-label="Route changed" className="border-b-2 border-amber bg-amber/10 px-3 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Stamp tone="warn">route changed</Stamp>
                  <span className="font-mono text-[11px] uppercase text-amber">{plan.route_notice.reason.replace('_', ' ')} · {plan.route_notice.kind}</span>
                </div>
                <p className="mt-1 text-xl font-semibold leading-snug">{plan.route_notice.message}</p>
                <p className="font-mono text-sm">
                  New ETA {Math.round(plan.route_notice.new_remaining_s / 60)} min ·{' '}
                  {Math.abs(plan.route_notice.saved_s) < 6 ? 'about the same time' : plan.route_notice.saved_s > 0 ? `${(plan.route_notice.saved_s / 60).toFixed(1)} min saved` : `${(Math.abs(plan.route_notice.saved_s) / 60).toFixed(1)} min longer, but necessary`}. Same destination.
                </p>
                <button className="btn-primary btn-touch mt-2 w-full" disabled={busy === 'ack'} onClick={() => ackNotice(plan.route_notice!.id)}>
                  Got it
                </button>
                <p className="mt-1 text-xs text-rule">This only confirms you saw it. The new route is already active.</p>
              </section>
            )}
            {plan?.reroute && (
              <section aria-label="Reroute" className="amber-pulse border-b-2 border-amber px-3 py-3">
                <div className="flex items-center gap-2"><Stamp tone="warn">reroute</Stamp><span className="font-mono text-[11px] uppercase text-amber">{label(plan.reroute.reason)}</span></div>
                <p className="mt-1 text-lg font-semibold leading-snug">{plan.reroute.message}</p>
                <p className="font-mono text-sm">New ETA {Math.round(plan.reroute.new_remaining_s / 60)} min ({plan.reroute.delta_s >= 0 ? '+' : ''}{Math.round(plan.reroute.delta_s / 60)} min). Not applied until you tap.</p>
                <div className="mt-2 grid grid-cols-[2fr_1fr] gap-2">
                  <button className="btn-primary btn-touch" disabled={busy === 'reroute'} onClick={() => reroute(plan.reroute!.id, true)}>Take new route</button>
                  <button className="btn-ghost btn-touch" disabled={busy === 'reroute'} onClick={() => reroute(plan.reroute!.id, false)}>Keep</button>
                </div>
              </section>
            )}

            {plan && (
              <section aria-label="Turn by turn" className="border-b hair px-3 py-3">
                <div className="flex items-center justify-between"><span className="rail-title">To {plan.target.kind === 'scene' ? 'scene' : 'hospital'}: {plan.target.name}</span><Stamp tone={plan.route.degraded ? 'warn' : 'mute'}>{plan.route.degraded ? 'degraded: mock routing' : plan.route.source}</Stamp></div>
                <div className="mt-2 flex items-center gap-3">
                  <Arrow maneuver={plan.next_step?.maneuver} />
                  <div className="min-w-0">
                    <div className="font-display text-5xl font-bold leading-none">{plan.distance_to_next_m != null ? dist(plan.distance_to_next_m) : '—'}</div>
                    <div className="text-xl font-semibold leading-tight">{plan.next_step?.instruction ?? 'Arriving'}</div>
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-2 border-t hair pt-2 font-mono">
                  <div><div className="text-[10px] uppercase text-rule">ETA range</div><div className="text-2xl font-semibold">{etaRange(plan.remaining_low_s, plan.remaining_high_s)}</div></div>
                  <div><div className="text-[10px] uppercase text-rule">Remaining</div><div className="text-2xl font-semibold">{dist(plan.remaining_m)}</div></div>
                </div>
              </section>
            )}

            {plan?.last_100m && plan.entrance && (
              <section aria-label="Entrance guidance" className="border-b-2 border-orange bg-orange/10 px-3 py-3">
                <div className="flex items-center gap-2"><Stamp tone="info">last 100 m</Stamp><span className="font-mono text-[11px] uppercase text-rule">entrance guidance</span></div>
                {plan.entrance.chosen ? (
                  <>
                    <p className="mt-1 font-display text-3xl font-bold uppercase leading-none">{plan.entrance.chosen.entrance.name}</p>
                    <p className="text-lg">{plan.entrance.chosen.entrance.kind} entrance{plan.entrance.chosen.entrance.landmark ? `, ${plan.entrance.chosen.entrance.landmark}` : ''}</p>
                    <div className="mt-1 flex flex-wrap gap-1"><Stamp tone={plan.entrance.chosen.entrance.accessible ? 'ok' : 'warn'}>{plan.entrance.chosen.entrance.accessible ? 'step-free' : 'steps'}</Stamp>{plan.entrance.chosen.entrance.gate_restricted && <Stamp tone="warn">gate restricted</Stamp>}</div>
                  </>
                ) : <p className="mt-1 text-lg">No usable entrance known.</p>}
                {plan.entrance.needs_confirmation && <p className="mt-2 border-l-4 border-amber bg-amber/10 px-2 py-1 text-sm">{plan.entrance.confirmation_request} Alternatives: {plan.entrance.alternatives.map((a) => a.entrance.name).join(', ') || 'none'}.</p>}
              </section>
            )}

            <section aria-label="Status" className="border-b hair px-3 py-3">
              <div className="rail-title mb-2">Status</div>
              {status === 'assigned' && <p className="mb-2 text-sm text-rule">Accept the job to start.</p>}
              <div className="grid grid-cols-2 gap-2">
                {FLOW.map((f) => {
                  const done = f.status === 'arrived' ? arrived : idx >= ORDER.indexOf(f.status)
                  const enabled = f.need.includes(status) && !(f.status === 'arrived' && arrived)
                  return (
                    <button key={f.status} className={`btn-touch ${done ? 'btn-ghost !border-green !text-green' : enabled ? 'btn-primary' : 'btn-ghost'}`} disabled={!enabled || busy === f.status} onClick={() => setStatus(f.status)} title={enabled ? '' : done ? 'Done' : 'Not available yet'}>
                      {done ? '✓ ' : ''}{f.label}
                    </button>
                  )
                })}
              </div>
              {arrived && <p className="mt-2 border-l-4 border-green bg-green/10 px-3 py-2 text-sm">Arrived at {inc?.hospital?.name}. Waiting for the hospital to complete handover ({label(inc?.handover_status ?? 'arrived')}). The unit is released only when they do.</p>}
              {actionErr && <div className="mt-2"><ErrorBox message={actionErr} /></div>}
            </section>

            <section aria-label="Position source" className="border-b hair px-3 py-3">
              <div className="rail-title mb-2">Position source</div>
              <div className="grid grid-cols-2 gap-2">
                <button className={`btn-touch ${gpsMode === 'sim' ? 'btn-primary' : 'btn-ghost'}`} aria-pressed={gpsMode === 'sim'} onClick={() => setGpsMode('sim')}>Simulated</button>
                <button className={`btn-touch ${gpsMode === 'device' ? 'btn-primary' : 'btn-ghost'}`} aria-pressed={gpsMode === 'device'} onClick={() => { setGpsMode('device'); if (simRunning) void sim() }}>Device GPS</button>
              </div>
              {gpsMode === 'sim' ? (
                <button className="btn-ghost btn-touch mt-2 w-full" onClick={sim} disabled={busy === 'sim' || !plan}>{simRunning ? 'Stop simulated movement' : 'Simulate movement along route'}</button>
              ) : (
                <p className="mt-2 text-sm text-rule">{gps.state === 'ok' ? `Sending device position (${gps.fix?.accuracy_m ? '±' + Math.round(gps.fix.accuracy_m) + ' m' : 'accuracy unknown'}).` : gps.state === 'denied' ? 'Location blocked. Positions are not being sent.' : 'Asking for location…'}</p>
              )}
              {gpsMode === 'sim' && simRunning && (
                <button className="btn-ghost btn-touch mt-2 w-full" onClick={wrongTurn} disabled={busy === 'wrong'} title="Demo only: pushes the simulated GPS off the route so the rejoin can be shown">
                  Simulate a wrong turn
                </button>
              )}
              {plan?.simulated_movement && <p className="mt-1 font-mono text-[11px] uppercase text-amber">This unit's movement is simulated</p>}
            </section>

            {plan && plan.green_corridor.signals.length > 0 && (
              <section aria-label="Green corridor simulation" className="px-3 py-3">
                <div className="flex items-center gap-2"><Stamp tone="warn">simulation</Stamp><span className="rail-title">Green corridor</span></div>
                <div className="mt-2 flex items-center gap-1" role="img" aria-label="Simulated traffic signals ahead">
                  <span className="chevron-move font-display text-3xl leading-none text-orange" aria-hidden="true">›</span>
                  {plan.green_corridor.signals.map((s) => {
                    const passed = s.along_m < plan.along_m - 10
                    const go = !passed && s.along_m < plan.along_m + 500
                    return <span key={s.id} title={`${s.id} ${passed ? 'passed' : go ? 'green' : 'red'}`} className={`grid h-8 w-8 place-items-center border-2 border-ink font-mono text-[10px] font-bold ${passed ? 'bg-rule/40 text-ink' : go ? 'bg-green text-bone' : 'bg-red text-bone'}`}>{passed ? '·' : go ? 'GO' : 'R'}</span>
                  })}
                </div>
                <p className="mt-2 text-xs text-rule">{plan.green_corridor.label}</p>
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
