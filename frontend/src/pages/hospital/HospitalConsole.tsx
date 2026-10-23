import { useCallback, useEffect, useMemo, useState } from 'react'
import { Ambulance, HazardBand } from '../../components/art'
import { Empty, ErrorBox, Loading, Section, Stamp, type Tone } from '../../components/ui'
import { api, ApiError, uid } from '../../lib/api'
import { useAuth } from '../../lib/auth'
import { useLiveRefresh } from '../../lib/events'
import { ageSince, ageText, clock, etaRange, label, mmss, valueText } from '../../lib/format'
import type { HospitalView, PreAlert } from '../../lib/types'

const STEPS: { key: string; label: string }[] = [
  { key: 'incoming', label: 'Pre-alert' },
  { key: 'acknowledged', label: 'Acknowledged' },
  { key: 'arrived', label: 'Arrived' },
  { key: 'handover_started', label: 'Handover started' },
  { key: 'handover_completed', label: 'Handover complete' },
]
const RANK: Record<string, number> = { incoming: 0, acknowledged: 1, arrived: 2, handover_started: 3, handover_completed: 4, superseded: -1 }
const SRC_TONE: Record<string, Tone> = { human_confirmed: 'ok', measured: 'ok', reported: 'info', ai_extracted: 'warn', simulated: 'mute' }
const RES_LABEL: Record<string, string> = {
  er_bay: 'ER bay',
  icu_bed: 'ICU bed',
  cath_lab: 'Cath lab',
  ct_scanner: 'CT scanner',
  trauma_team: 'Trauma team',
  nicu_cot: 'NICU cot',
}

export default function HospitalConsole() {
  const { user } = useAuth()
  const [alerts, setAlerts] = useState<PreAlert[] | null>(null)
  const [hosp, setHosp] = useState<HospitalView | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [, setTick] = useState(0)
  const [counts, setCounts] = useState<Record<string, string>>({})
  const [notes, setNotes] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    try {
      const [a, h] = await Promise.all([api.get<PreAlert[]>('/api/prealerts'), api.get<HospitalView[]>('/api/hospitals')])
      setAlerts(a)
      setHosp(h[0] ?? null)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])
  useLiveRefresh(load, { pollMs: 3000 })
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const live = useMemo(() => (alerts ?? []).filter((a) => a.status !== 'superseded' && a.status !== 'handover_completed'), [alerts])
  useEffect(() => {
    if (!sel && alerts && alerts.length) setSel((live[0] ?? alerts[0]).id)
  }, [alerts, live, sel])
  const cur = alerts?.find((a) => a.id === sel) ?? null

  const act = async (name: string, path: string, body?: unknown) => {
    setBusy(name)
    setActionErr(null)
    try {
      await api.post(path, body)
      await load()
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const setResource = async (resource: string, status: 'available' | 'unavailable' | 'unknown') => {
    if (!hosp) return
    setBusy(`${resource}-${status}`)
    setActionErr(null)
    try {
      const n = counts[resource]
      await api.patch(`/api/hospitals/${hosp.id}/resources`, {
        update_id: uid(),
        occurred_at: new Date().toISOString(),
        resource,
        status,
        count: status === 'available' && n ? Number(n) : null,
        source: 'human_confirmed',
        reason: `${user?.name ?? 'hospital desk'} marked ${status}`,
      })
      await load()
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : 'Update failed')
    } finally {
      setBusy(null)
    }
  }

  if (!alerts && !err) return <Loading what="Loading console" />

  const rank = cur ? RANK[cur.status] : -1
  const handoverSecs = cur?.arrived_at ? (cur.handover_completed_at ? (cur.handover_duration_s ?? 0) : (ageSince(cur.arrived_at) ?? 0)) : null

  return (
    <div className="flex min-h-full flex-col bg-ink">
      <HazardBand height={10} dark />
      <div className="flex flex-wrap items-center gap-3 border-b hair px-3 py-2">
        <h1 className="font-display text-4xl font-bold uppercase leading-none">{hosp?.name ?? user?.name}</h1>
        <Stamp tone="mute">{user?.hospital_id}</Stamp>
        <span className="font-mono text-xs uppercase text-rule">Only cases assigned to this hospital are shown</span>
        <span className="ml-auto font-display text-3xl font-bold text-orange">{live.length}<span className="ml-2 font-mono text-xs font-normal uppercase text-rule">incoming / active</span></span>
      </div>
      {err && <div className="p-3"><ErrorBox message={err} onRetry={load} /></div>}

      <div className="grid min-h-0 flex-1 lg:grid-cols-[400px_minmax(0,1fr)_380px]">
        {/* incoming list */}
        <aside aria-label="Incoming pre-alerts" className="border-b hair lg:border-b-0 lg:border-r">
          <div className="rail-title border-b hair px-3 py-2">Incoming pre-alerts</div>
          {alerts && alerts.length === 0 ? (
            <div className="p-3"><Empty title="No cases assigned" hint="A pre-alert appears here when a unit is dispatched to this hospital." /></div>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="w-1" />
                  <th className="py-1 pl-2">Case</th>
                  <th>Reason</th>
                  <th>ETA</th>
                  <th className="pr-2 text-right">State</th>
                </tr>
              </thead>
              <tbody>
                {alerts!.map((a) => {
                  const dead = a.status === 'superseded'
                  const tone: Tone = dead ? 'mute' : a.status === 'incoming' ? 'warn' : a.status === 'handover_completed' ? 'ok' : 'info'
                  return (
                    <tr key={a.id} onClick={() => setSel(a.id)} className={`cursor-pointer border-b hair ${a.id === sel ? 'bg-bone/15' : 'hover:bg-bone/5'} ${dead ? 'opacity-60' : ''}`}>
                      <td className={`w-1 p-0 ${dead ? 'bg-rule/50' : a.status === 'incoming' ? 'bg-amber' : a.status === 'handover_completed' ? 'bg-green' : 'bg-orange'}`} />
                      <td className="py-2 pl-2">
                        <button className="text-left" aria-pressed={a.id === sel} aria-label={`Open case ${a.incident_id}`}>
                          <span className="block font-mono font-semibold">{a.incident_id}</span>
                          <span className="block font-mono text-[10px] text-rule">{a.vehicle_id}</span>
                        </button>
                      </td>
                      <td className="max-w-[110px] text-xs leading-tight">{a.reported_reason}</td>
                      <td className="font-mono text-xs">{a.arrived_at ? 'here' : etaRange(a.eta_low_s, a.eta_high_s)}</td>
                      <td className="pr-2 text-right"><Stamp tone={tone}>{dead ? 'redirected' : label(a.status)}</Stamp></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </aside>

        {/* selected case */}
        <section aria-label="Case detail" className="min-w-0 border-b hair lg:border-b-0 lg:border-r">
          {!cur ? (
            <div className="grid h-full place-items-center p-8"><div className="text-center"><Ambulance className="mx-auto h-24 w-auto opacity-80" /><p className="mt-3 font-display text-3xl font-bold uppercase">Waiting for a pre-alert</p></div></div>
          ) : (
            <>
              <div className="border-b hair px-3 py-3">
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="font-display text-5xl font-bold leading-none">{cur.incident_id}</h2>
                  <Stamp tone="mute">{cur.id}</Stamp>
                  <Stamp tone="mute">unit {cur.vehicle_id}</Stamp>
                  <Stamp tone="mute">simulated pre-alert</Stamp>
                </div>
                <p className="mt-1 text-xl font-semibold">{cur.reported_reason}</p>
                {cur.status === 'superseded' ? (
                  <p role="status" className="mt-2 border-l-4 border-amber bg-amber/10 px-3 py-2 text-sm">This patient has been redirected: {cur.superseded_reason}. Nothing further is needed from this hospital.</p>
                ) : (
                  <div className="mt-2 flex flex-wrap items-end gap-x-8 gap-y-2">
                    <div><div className="font-mono text-[10px] uppercase text-rule">{cur.arrived_at ? 'Arrived' : 'ETA range'}</div><div className="font-display text-5xl font-bold leading-none text-orange">{cur.arrived_at ? clock(cur.arrived_at) : etaRange(cur.eta_low_s, cur.eta_high_s)}</div></div>
                    {handoverSecs !== null && <div><div className="font-mono text-[10px] uppercase text-rule">{cur.handover_completed_at ? 'Arrival to handover (simulated)' : 'Waiting since arrival'}</div><div className="font-mono text-4xl font-semibold leading-none">{mmss(handoverSecs)}</div></div>}
                  </div>
                )}
              </div>

              <ol aria-label="Case progress" className="grid grid-cols-5 border-b hair">
                {STEPS.map((s, i) => {
                  const done = s.key === 'acknowledged' ? !!cur.acknowledged_at : rank >= i
                  return (
                    <li key={s.key} className={`border-r hair px-2 py-2 last:border-r-0 ${done ? 'bg-green/10' : ''}`}>
                      <div className="flex items-center gap-1.5"><span aria-hidden="true" className={`grid h-4 w-4 place-items-center border text-[10px] ${done ? 'border-green bg-green text-bone' : 'border-rule/50'}`}>{done ? '✓' : ''}</span><span className="font-mono text-[10px] uppercase leading-tight">{s.label}</span></div>
                    </li>
                  )
                })}
              </ol>

              {cur.status !== 'superseded' && (
                <div className="border-b hair px-3 py-3">
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    <button className="btn-primary" disabled={!!cur.acknowledged_at || busy === 'ack'} onClick={() => act('ack', `/api/prealerts/${cur.id}/acknowledge`)}>{cur.acknowledged_at ? '✓ ' : ''}Acknowledge</button>
                    <button className="btn-primary" disabled={!!cur.arrived_at || busy === 'arr'} onClick={() => act('arr', `/api/prealerts/${cur.id}/arrival`)} title={cur.arrived_at ? `Arrival recorded by ${cur.arrival_source}` : 'Confirm the unit has arrived'}>{cur.arrived_at ? '✓ ' : ''}Arrival</button>
                    <button className="btn-primary" disabled={rank !== 2 || busy === 'hs'} onClick={() => act('hs', `/api/prealerts/${cur.id}/handover_started`)} title={rank < 2 ? 'Arrival must be recorded first' : ''}>{rank >= 3 ? '✓ ' : ''}Handover started</button>
                    <button className="btn-go" disabled={rank !== 3 || busy === 'hc'} onClick={() => act('hc', `/api/prealerts/${cur.id}/handover_completed`)} title={rank < 3 ? 'Start the handover first' : ''}>{rank >= 4 ? '✓ ' : ''}Handover complete</button>
                  </div>
                  <p className="mt-1 text-xs text-rule">Steps must happen in order. Completing the handover frees the ambulance and closes the case. Every step is audited.</p>
                  {actionErr && <div className="mt-2"><ErrorBox message={actionErr} /></div>}
                </div>
              )}

              <Section title="Reported" right={<span className="font-mono text-[10px] uppercase text-rule">caller / extracted: not confirmed</span>}>
                {cur.reported_observations.length === 0 ? <p className="text-sm text-rule">Nothing reported beyond the reason above.</p> : (
                  <ul>{cur.reported_observations.map((o) => (
                    <li key={o.key} className="flex items-baseline justify-between gap-2 border-b hair py-1 last:border-0"><span className="font-mono text-[11px] uppercase text-rule">{label(o.key)}</span><span className="font-semibold">{valueText(o.value)}</span><Stamp tone={SRC_TONE[o.source]}>{label(o.source)}</Stamp><span className="font-mono text-[10px] text-rule">{clock(o.at)}</span></li>
                  ))}</ul>
                )}
              </Section>
              <Section title="Confirmed" right={<span className="font-mono text-[10px] uppercase text-rule">human or measured</span>}>
                {cur.confirmed_observations.length === 0 ? <p className="text-sm text-rule">Nothing confirmed yet. Treat everything above as unverified.</p> : (
                  <ul>{cur.confirmed_observations.map((o) => (
                    <li key={o.key} className="flex items-baseline justify-between gap-2 border-b hair py-1 last:border-0"><span className="font-mono text-[11px] uppercase text-rule">{label(o.key)}</span><span className="font-semibold">{valueText(o.value)}</span><Stamp tone={SRC_TONE[o.source]}>{label(o.source)}</Stamp><span className="font-mono text-[10px] text-rule">{clock(o.at)}</span></li>
                  ))}</ul>
                )}
              </Section>
              <Section title="Unknown">
                {cur.unknowns.length === 0 ? <p className="text-sm text-green">No open unknowns.</p> : <div className="flex flex-wrap gap-1.5">{cur.unknowns.map((u) => <Stamp key={u} tone="warn">? {u}</Stamp>)}</div>}
              </Section>
              <Section title="Missing-data checklist" right={<span className="font-mono text-[10px] uppercase text-rule">{cur.checklist.filter((c) => c.status === 'open').length} open</span>}>
                <ul>
                  {cur.checklist.map((c) => (
                    <li key={c.key} className="border-b hair py-1.5 last:border-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Stamp tone={c.status === 'open' ? 'warn' : c.status === 'provided' ? 'ok' : 'mute'}>{c.status === 'not_applicable' ? 'n/a' : c.status}</Stamp>
                        <span className="min-w-0 flex-1 text-sm">{c.label}</span>
                        {cur.status !== 'superseded' && (
                          <span className="flex gap-1">
                            <button className="btn-ghost !min-h-[30px] !px-2 !text-sm" disabled={c.status === 'provided' || busy === `cl-${c.key}`} onClick={() => act(`cl-${c.key}`, `/api/prealerts/${cur.id}/checklist`, { key: c.key, status: 'provided', note: notes[c.key] || null })}>Provided</button>
                            <button className="btn-ghost !min-h-[30px] !px-2 !text-sm" disabled={c.status === 'not_applicable' || busy === `cl-${c.key}`} onClick={() => act(`cl-${c.key}`, `/api/prealerts/${cur.id}/checklist`, { key: c.key, status: 'not_applicable' })}>N/A</button>
                            <button className="btn-ghost !min-h-[30px] !px-2 !text-sm" disabled={c.status === 'open' || busy === `cl-${c.key}`} onClick={() => act(`cl-${c.key}`, `/api/prealerts/${cur.id}/checklist`, { key: c.key, status: 'open' })}>Open</button>
                          </span>
                        )}
                      </div>
                      {c.status === 'open' && cur.status !== 'superseded' && (
                        <input aria-label={`Note for ${c.label}`} value={notes[c.key] ?? ''} onChange={(e) => setNotes({ ...notes, [c.key]: e.target.value })} placeholder="Note (optional)" className="mt-1 min-h-[32px] w-full border border-rule/40 bg-ink px-2 text-sm" />
                      )}
                      {c.note && c.status !== 'open' && <p className="mt-0.5 text-xs text-rule">{c.note}</p>}
                    </li>
                  ))}
                </ul>
              </Section>
            </>
          )}
        </section>

        {/* resources */}
        <aside aria-label="Resource freshness" className="min-w-0">
          <div className="rail-title border-b hair px-3 py-2">Capabilities and resources</div>
          {hosp ? (
            <>
              <div className="border-b hair px-3 py-2">
                <div className="font-mono text-[10px] uppercase text-rule">Stable capabilities (do not change shift to shift)</div>
                <div className="mt-1 flex flex-wrap gap-1">{hosp.capabilities.map((c) => <Stamp key={c} tone="mute">{c}</Stamp>)}</div>
              </div>
              <div className="px-3 py-2 font-mono text-[10px] uppercase text-rule">Current resources: confirm, deny, or mark unknown. Stale data counts as unknown, never available.</div>
              <ul>
                {Object.entries(hosp.resources).map(([k, r]) => {
                  const tone: Tone = r.status === 'available' ? 'ok' : r.status === 'unavailable' ? 'bad' : 'warn'
                  return (
                    <li key={k} className="border-b hair px-3 py-2">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-lg font-semibold">{RES_LABEL[k] ?? label(k)}</span>
                        <span className="flex items-center gap-1.5">
                          {r.stale && <Stamp tone="warn" title="Not re-confirmed recently">stale</Stamp>}
                          <Stamp tone={tone}>{r.status}</Stamp>
                        </span>
                      </div>
                      <div className="font-mono text-[11px] text-rule">
                        confirmed {r.age_s == null ? 'never' : `${ageText(r.age_s)} ago`} · {label(r.source)}
                        {r.stale && r.reported_status === 'available' && <span className="text-amber"> · reported available, shown unknown</span>}
                        {r.count != null && ` · count ${r.count}`}
                      </div>
                      <div className="mt-1.5 grid grid-cols-[1fr_1fr_1fr_64px] gap-1">
                        <button className="btn-go !min-h-[36px] !px-1 !text-sm" disabled={busy !== null} onClick={() => setResource(k, 'available')}>Confirm</button>
                        <button className="btn-danger !min-h-[36px] !px-1 !text-sm" disabled={busy !== null} onClick={() => setResource(k, 'unavailable')}>Deny</button>
                        <button className="btn-ghost !min-h-[36px] !px-1 !text-sm" disabled={busy !== null} onClick={() => setResource(k, 'unknown')}>Unknown</button>
                        <input aria-label={`Count for ${RES_LABEL[k] ?? k}`} type="number" min={0} placeholder="n" value={counts[k] ?? ''} onChange={(e) => setCounts({ ...counts, [k]: e.target.value })} className="min-h-[36px] w-full border border-rule/40 bg-ink px-1 text-center font-mono text-sm" />
                      </div>
                    </li>
                  )
                })}
              </ul>
            </>
          ) : <Loading what="Loading resources" />}
        </aside>
      </div>
    </div>
  )
}
