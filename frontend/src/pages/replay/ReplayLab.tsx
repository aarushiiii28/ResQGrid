import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Circle, Marker, Polyline, Tooltip } from 'react-leaflet'
import { BarChart, DotStrip, Legend, POLICY_COLOR, POLICY_NAME } from '../../components/charts'
import MapView from '../../components/MapView'
import { hospitalIcon, incidentIcon, vehicleIcon } from '../../components/markers'
import { Empty, ErrorBox, Section, Stamp } from '../../components/ui'
import { api, ApiError, download } from '../../lib/api'
import { mmss } from '../../lib/format'
import type { Benchmark, SimResult, SimRun } from '../../lib/types'
import FalseAlarmLab from './FalseAlarmLab'
import RouteLab from './RouteLab'

const FAULTS: { key: string; label: string; text: string }[] = [
  { key: 'duplicate_calls', label: 'Duplicate calls', text: 'Several callers report the same event.' },
  { key: 'stale_hospital_data', label: 'Stale hospital data', text: 'A hospital stops reporting, then quietly goes down.' },
  { key: 'blocked_entrance', label: 'Blocked entrance', text: 'The nearest entrance is blocked. Crews lose time unless told.' },
  { key: 'route_closure', label: 'Route closure', text: 'A road closes near an incident.' },
  { key: 'no_acknowledgment', label: 'No acknowledgment', text: 'The first crew sent never answers.' },
  { key: 'stale_gps', label: 'Stale GPS', text: 'Two real outages and three short blips that are false alarms.' },
  { key: 'offline_unit', label: 'Offline unit', text: 'A unit drops off the network, maybe mid-job.' },
  { key: 'competing_incident', label: 'Competing incident', text: 'A burst of calls ends with a P1 while units are busy.' },
]

const ROWS: { key: string; label: string; lower: boolean; fmt: 'int' | 's' | 'pct' | 'share' }[] = [
  { key: 'response_median_s', label: 'Response time, median', lower: true, fmt: 's' },
  { key: 'response_p90_s', label: 'Response time, p90', lower: true, fmt: 's' },
  { key: 'p1_response_median_s', label: 'Response time, P1 calls, median', lower: true, fmt: 's' },
  { key: 'dest_median_s', label: 'Time to appropriate destination, median', lower: true, fmt: 's' },
  { key: 'dest_p90_s', label: 'Time to appropriate destination, p90', lower: true, fmt: 's' },
  { key: 'pct_meeting_requirements', label: 'Meeting requirements', lower: false, fmt: 'pct' },
  { key: 'unserved', label: 'Calls never attended', lower: true, fmt: 'int' },
  { key: 'wasted_dispatches', label: 'Wasted dispatches', lower: true, fmt: 'int' },
  { key: 'unnecessary_reassignments', label: 'Unnecessary reassignments', lower: true, fmt: 'int' },
  { key: 'oscillations', label: 'Plan oscillations', lower: true, fmt: 'int' },
  { key: 'preemptions', label: 'Deliberate hand-overs', lower: true, fmt: 'int' },
  { key: 'late_acks', label: 'Late acknowledgments (>60 s)', lower: true, fmt: 'int' },
  { key: 'missed_acks', label: 'Missed acknowledgments', lower: true, fmt: 'int' },
  { key: 'dispatch_to_accept_median_s', label: 'Dispatch to accept, median', lower: true, fmt: 's' },
  { key: 'arrival_to_handover_median_s', label: 'Arrival to handover, median', lower: true, fmt: 's' },
  { key: 'coverage_mean', label: 'Coverage after dispatch', lower: false, fmt: 'share' },
  { key: 'eta_calibration', label: 'ETA range calibration', lower: false, fmt: 'share' },
  { key: 'diversions', label: 'Hospital diversions', lower: true, fmt: 'int' },
  { key: 'detection_rate', label: 'Failures detected', lower: false, fmt: 'share' },
  { key: 'detect_median_s', label: 'Time to detect failure, median', lower: true, fmt: 's' },
  { key: 'decide_median_s', label: 'Time to recovery decision, median', lower: true, fmt: 's' },
]

function show(v: number | null | undefined, f: string) {
  if (v === null || v === undefined) return 'n/a'
  if (f === 's') return mmss(v) + ` (${Math.round(v)} s)`
  if (f === 'pct') return `${v.toFixed(1)}%`
  if (f === 'share') return `${(v * 100).toFixed(1)}%`
  return String(Math.round(v * 100) / 100)
}

function ReplayMap({ run, policy, t }: { run: SimRun; policy: string; t: number }) {
  const res = run.results[policy]
  const idx = (n: number) => Math.min(Math.floor(t / 60), n - 1)
  const arrived = new Set(res.timeline.filter((e) => e.kind === 'arrive_scene' && e.t <= t).map((e) => e.incident))
  const incidents = run.world.incidents.filter((i) => i.t_call <= t)
  const pos = (vid: string): [number, number] => {
    const tr = res.tracks[vid]
    return tr[idx(tr.length)] as [number, number]
  }
  const lines = res.timeline.filter((e) => e.kind === 'dispatch' && e.t <= t && e.vehicle).filter((e) => {
    const done = res.timeline.some((x) => x.t <= t && x.vehicle === e.vehicle && x.t > e.t && ['arrive_scene', 'release', 'wasted'].includes(x.kind))
    return !done
  })
  const incById = Object.fromEntries(run.world.incidents.map((i) => [i.id, i]))
  const fit = useMemo(() => [...run.world.hospitals, ...run.world.incidents].map((p) => [p.lat, p.lon] as [number, number]), [run])
  return (
    <MapView className="h-[340px] border hair" zoom={12} fit={fit} fitKey={run.id} label={`${POLICY_NAME[policy]} map`}>
      {run.world.closures.filter((c) => c.t0 <= t && t < c.t1).map((c) => (
        <Circle key={c.id} center={[c.lat, c.lon]} radius={c.radius_m} pathOptions={{ color: '#C0182B', weight: 3, dashArray: '8 6', fillOpacity: 0.15 }}><Tooltip>closure {c.id}</Tooltip></Circle>
      ))}
      {run.world.hospitals.map((h) => <Marker key={h.id} position={[h.lat, h.lon]} icon={hospitalIcon({ stale: false })}><Tooltip>{h.id} {h.caps.join(', ')}</Tooltip></Marker>)}
      {lines.map((e) => {
        const inc = incById[e.incident ?? '']
        return inc && e.vehicle ? <Polyline key={`${e.vehicle}-${e.t}`} positions={[pos(e.vehicle), [inc.lat, inc.lon]]} pathOptions={{ color: POLICY_COLOR[policy] === '#15120E' ? '#E4570F' : POLICY_COLOR[policy], weight: 3, dashArray: '6 6' }} /> : null
      })}
      {incidents.map((i) => (
        <Marker key={i.id} position={[i.lat, i.lon]} icon={incidentIcon({ priority: i.priority, n: arrived.has(i.id) || arrived.has(i.duplicate_of ?? '') ? '✓' : String(i.priority) })}>
          <Tooltip>{i.id} {i.category} P{i.priority}{i.duplicate_of ? ` (duplicate of ${i.duplicate_of})` : ''}{arrived.has(i.id) ? ' · attended' : ''}</Tooltip>
        </Marker>
      ))}
      {run.world.vehicles.map((v) => {
        const p = pos(v.id)
        const prev = res.tracks[v.id][Math.max(0, idx(res.tracks[v.id].length) - 1)]
        const moving = Math.abs(p[0] - prev[0]) + Math.abs(p[1] - prev[1]) > 1e-6
        return <Marker key={v.id} position={p} icon={vehicleIcon({ id: v.id, status: moving ? 'en_route' : 'available', stale: false, simulated: true })} />
      })}
    </MapView>
  )
}

function Log({ res, t }: { res: SimResult; t: number }) {
  const ev = res.timeline.filter((e) => e.t <= t).slice(-9).reverse()
  return (
    <ol aria-label={`${res.policy} event log`} className="h-[210px] overflow-auto border hair font-mono text-[11px]">
      {ev.length === 0 && <li className="p-2 text-rule">Nothing yet.</li>}
      {ev.map((e, i) => (
        <li key={`${e.t}-${i}`} className="flex gap-2 border-b hair px-2 py-0.5">
          <span className="w-12 shrink-0 text-rule">{mmss(e.t)}</span>
          <span className={`w-28 shrink-0 uppercase ${e.kind.startsWith('risk') ? 'text-amber' : e.kind === 'diverted' || e.kind === 'unit_lost' || e.kind === 'wasted' ? 'text-red' : e.kind === 'reroute' || e.kind === 'redirect' ? 'text-green' : ''}`}>{e.kind.replace(/_/g, ' ')}</span>
          <span className="min-w-0">{e.incident ? `${e.incident} ` : ''}{e.detail}</span>
        </li>
      ))}
    </ol>
  )
}

type Lab = 'dispatch' | 'false' | 'routes'

export default function ReplayLab() {
  const [lab, setLab] = useState<Lab>('dispatch')
  const [seed, setSeed] = useState('42')
  const [faults, setFaults] = useState<Set<string>>(new Set(FAULTS.map((f) => f.key)))
  const [run, setRun] = useState<SimRun | null>(null)
  const [bench, setBench] = useState<Benchmark | null>(null)
  const [benchN, setBenchN] = useState('10')
  const [t, setT] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [left, setLeft] = useState('P0')
  const [right, setRight] = useState('P2')
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setInterval> | undefined>(undefined)

  const end = run ? Math.max(...Object.values(run.results).map((r) => r.end_s)) : 0
  useEffect(() => {
    if (!playing) return
    timer.current = setInterval(() => setT((x) => (x + 45 >= end ? (setPlaying(false), end) : x + 45)), 350)
    return () => clearInterval(timer.current)
  }, [playing, end])

  const request = useCallback(() => ({ seed: Number(seed), faults: Array.from(faults) }), [seed, faults])
  const go = async () => {
    setBusy('run')
    setErr(null)
    setPlaying(false)
    try {
      const r = await api.post<SimRun>('/api/simulation/runs', request())
      setRun(r)
      setT(0)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Run failed')
    } finally {
      setBusy(null)
    }
  }
  const goBench = async () => {
    setBusy('bench')
    setErr(null)
    try {
      setBench(await api.post<Benchmark>('/api/simulation/benchmark', { seeds: Number(benchN), faults: Array.from(faults) }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Benchmark failed')
    } finally {
      setBusy(null)
    }
  }
  const exp = (format: 'json' | 'csv', part: string) => run && download(`/api/simulation/runs/${run.id}/export?format=${format}&part=${part}`, `${run.id}-${part}.${format}`).catch((e) => setErr(e.message))

  const pols = run ? Object.keys(run.results) : []
  const bests = (key: string, lower: boolean) => {
    const vals = pols.map((p) => [p, run!.results[p].metrics[key]] as const).filter(([, v]) => v !== null && v !== undefined) as [string, number][]
    if (vals.length < 2) return new Set<string>()
    const best = lower ? Math.min(...vals.map((v) => v[1])) : Math.max(...vals.map((v) => v[1]))
    const worst = lower ? Math.max(...vals.map((v) => v[1])) : Math.min(...vals.map((v) => v[1]))
    return best === worst ? new Set<string>() : new Set(vals.filter((v) => v[1] === best).map((v) => v[0]))
  }
  const m = (p: string, k: string) => run?.results[p]?.metrics[k] as number | null | undefined
  const marks = run ? run.world.incidents.filter((i) => !i.duplicate_of).map((i) => i.t_call) : []

  return (
    <div className="grid min-h-full lg:grid-cols-[330px_minmax(0,1fr)]">
      <aside aria-label="Run controls" className="border-b hair lg:border-b-0 lg:border-r">
        <div className="border-b hair px-3 py-3">
          <h1 className="font-display text-4xl font-bold uppercase leading-none">Replay lab</h1>
          <p className="mt-1 text-xs text-rule">Same seeded world, three policies. Every number here is <b>simulated</b>.</p>
        </div>
        <div className="space-y-3 border-b hair px-3 py-3">
          <label className="block font-mono text-[10px] uppercase text-rule">Seed
            <div className="mt-0.5 flex gap-1">
              <input aria-label="Seed" type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} className="min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-base" />
              <button className="btn-ghost !min-h-[40px] !px-3 !text-base" onClick={() => setSeed(String(Math.floor(Math.random() * 100000)))}>Random</button>
            </div>
          </label>
          <fieldset>
            <legend className="mb-1 font-mono text-[10px] uppercase text-rule">Fault injection</legend>
            <ul>
              {FAULTS.map((f) => (
                <li key={f.key} className="border-b hair py-1">
                  <label className="flex cursor-pointer items-start gap-2">
                    <input type="checkbox" className="mt-1 h-4 w-4 accent-orange" checked={faults.has(f.key)} onChange={(e) => { const n = new Set(faults); e.target.checked ? n.add(f.key) : n.delete(f.key); setFaults(n) }} />
                    <span><span className="text-sm font-semibold">{f.label}</span><span className="block text-xs text-rule">{f.text}</span></span>
                  </label>
                </li>
              ))}
            </ul>
            <div className="mt-1 flex gap-2 font-mono text-[11px] uppercase">
              <button className="underline" onClick={() => setFaults(new Set(FAULTS.map((f) => f.key)))}>all</button>
              <button className="underline" onClick={() => setFaults(new Set())}>none</button>
            </div>
          </fieldset>
          <button className="btn-primary w-full" onClick={go} disabled={busy !== null}>{busy === 'run' ? 'Running…' : 'Run simulation'}</button>
          <div className="flex items-end gap-2">
            <label className="w-24 font-mono text-[10px] uppercase text-rule">Seeds
              <input aria-label="Benchmark seeds" type="number" min={2} max={40} value={benchN} onChange={(e) => setBenchN(e.target.value)} className="mt-0.5 min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-base" />
            </label>
            <button className="btn-ghost flex-1" onClick={goBench} disabled={busy !== null}>{busy === 'bench' ? 'Running…' : 'Run benchmark'}</button>
          </div>
          {err && <ErrorBox message={err} />}
        </div>
        {run && (
          <div className="px-3 py-3">
            <div className="rail-title mb-1">Export {run.id}</div>
            <div className="grid grid-cols-2 gap-2">
              <button className="btn-ghost !text-base" onClick={() => exp('json', 'full')}>JSON</button>
              <button className="btn-ghost !text-base" onClick={() => exp('csv', 'metrics')}>CSV metrics</button>
              <button className="btn-ghost !text-base" onClick={() => exp('csv', 'incidents')}>CSV incidents</button>
              <button className="btn-ghost !text-base" onClick={() => exp('csv', 'timeline')}>CSV timeline</button>
            </div>
            <p className="mt-2 font-mono text-[10px] text-rule">Saved with seed, config, policy versions, timeline and metrics.</p>
          </div>
        )}
      </aside>

      <section aria-label="Results" className="min-w-0">
        <div role="note" className="flex flex-wrap items-center gap-3 border-b-2 border-amber bg-amber/10 px-4 py-2">
          <Stamp tone="warn">simulated</Stamp>
          <span className="text-sm">Results describe a synthetic world with toy policies. They are not predictions of real emergencies and make no claim about lives saved.</span>
        </div>
        <div role="tablist" aria-label="Lab" className="flex border-b hair">
          {([['dispatch', 'Dispatch policies'], ['false', 'False alarms'], ['routes', 'Stable routes']] as [Lab, string][]).map(([k, l]) => (
            <button key={k} role="tab" aria-selected={lab === k} onClick={() => setLab(k)} className={`label min-h-[40px] border-r hair px-4 text-lg ${lab === k ? 'bg-bone text-ink' : 'hover:bg-bone/10'}`}>{l}</button>
          ))}
        </div>
        {lab === 'false' && <FalseAlarmLab />}
        {lab === 'routes' && <RouteLab />}
        {lab === 'dispatch' && !run && !bench ? (
          <div className="p-6"><Empty title="Run a simulation" hint="Pick a seed and faults, then run. The same seed always gives the same events and metrics." /></div>
        ) : null}

        {lab === 'dispatch' && run && (
          <>
            <div className="border-b hair px-4 py-3">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="font-display text-3xl font-bold uppercase leading-none">{run.id} · seed {run.seed}</h2>
                {pols.map((p) => <Stamp key={p} tone="mute" title={run.results[p].description}>{run.policy_versions[p]}</Stamp>)}
              </div>
              <div className="mt-3 flex items-center gap-3">
                <button className="btn-ghost !min-h-[40px] !px-4" aria-pressed={playing} onClick={() => { if (t >= end) setT(0); setPlaying(!playing) }}>{playing ? 'Pause' : 'Play'}</button>
                <div className="relative flex-1">
                  <input aria-label="Timeline scrubber" type="range" min={0} max={Math.max(end, 1)} step={15} value={t} onChange={(e) => { setPlaying(false); setT(Number(e.target.value)) }} className="h-8 w-full accent-orange" />
                  <div className="pointer-events-none absolute inset-x-0 top-0 h-2">
                    {marks.map((x) => <span key={x} className="absolute top-0 h-2 w-0.5 bg-orange" style={{ left: `${(x / Math.max(end, 1)) * 100}%` }} />)}
                    {run.world.closures.map((c) => <span key={c.id} className="absolute top-0 h-2 bg-red/60" style={{ left: `${(c.t0 / Math.max(end, 1)) * 100}%`, width: `${((c.t1 - c.t0) / Math.max(end, 1)) * 100}%` }} />)}
                  </div>
                </div>
                <span className="w-16 font-mono text-lg font-semibold">{mmss(t)}</span>
              </div>
              <p className="font-mono text-[10px] uppercase text-rule">orange ticks: calls · red bands: road closures · sim clock starts at 08:00</p>
            </div>

            <div className="grid gap-px border-b hair bg-rule/30 xl:grid-cols-2">
              {[left, right].map((p, i) => (
                <div key={i} className="bg-ink p-3">
                  <div className="mb-2 flex items-center gap-2">
                    <select aria-label={i === 0 ? 'Left policy' : 'Right policy'} className="min-h-[36px] border border-rule/60 bg-ink px-2 font-display text-xl font-bold uppercase" value={p} onChange={(e) => (i === 0 ? setLeft : setRight)(e.target.value)}>
                      {pols.map((x) => <option key={x} value={x}>{POLICY_NAME[x]}</option>)}
                    </select>
                    <span className="inline-block h-3 w-3" style={{ background: POLICY_COLOR[p], border: '1px solid #BDB5A3' }} />
                    <span className="font-mono text-[11px] text-rule">{String(m(p, 'response_median_s') != null ? `median response ${mmss(m(p, 'response_median_s')!)}` : '')}</span>
                  </div>
                  <ReplayMap run={run} policy={p} t={t} />
                  <Log res={run.results[p]} t={t} />
                </div>
              ))}
            </div>

            <div className="grid gap-px border-b hair bg-rule/30 xl:grid-cols-2">
              <div className="bg-ink p-3">
                <Legend policies={pols} />
                <div className="mt-2">
                  <BarChart title="Response and destination time (minutes)" policies={pols} unit=" min"
                    bars={[
                      { label: 'Response median', values: Object.fromEntries(pols.map((p) => [p, m(p, 'response_median_s') != null ? m(p, 'response_median_s')! / 60 : null])) },
                      { label: 'Response p90', values: Object.fromEntries(pols.map((p) => [p, m(p, 'response_p90_s') != null ? m(p, 'response_p90_s')! / 60 : null])) },
                      { label: 'To destination p50', values: Object.fromEntries(pols.map((p) => [p, m(p, 'dest_median_s') != null ? m(p, 'dest_median_s')! / 60 : null])) },
                    ]} />
                </div>
              </div>
              <div className="bg-ink p-3">
                <Legend policies={pols} />
                <div className="mt-2">
                  <BarChart title="Quality and recovery (percent)" policies={pols} unit="%"
                    bars={[
                      { label: 'Meeting requirements', values: Object.fromEntries(pols.map((p) => [p, m(p, 'pct_meeting_requirements')])) },
                      { label: 'Failures detected', values: Object.fromEntries(pols.map((p) => [p, m(p, 'detection_rate') != null ? m(p, 'detection_rate')! * 100 : null])) },
                      { label: 'Coverage kept', values: Object.fromEntries(pols.map((p) => [p, m(p, 'coverage_mean') != null ? m(p, 'coverage_mean')! * 100 : null])) },
                      { label: 'ETA range hit', values: Object.fromEntries(pols.map((p) => [p, m(p, 'eta_calibration') != null ? m(p, 'eta_calibration')! * 100 : null])) },
                    ]} />
                </div>
              </div>
            </div>

            <Section title="Metrics" right={<span className="font-mono text-[10px] uppercase text-rule">green edge: best · this one seed only</span>}>
              <table className="w-full border-collapse text-sm">
                <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Metric</th>{pols.map((p) => <th key={p} className="pr-2">{POLICY_NAME[p]}</th>)}</tr></thead>
                <tbody>
                  {ROWS.map((r) => {
                    const best = bests(r.key, r.lower)
                    return (
                      <tr key={r.key} className="border-b hair">
                        <td className="py-1.5">{r.label}</td>
                        {pols.map((p) => (
                          <td key={p} className={`pr-2 font-mono text-xs ${best.has(p) ? 'border-l-4 border-green pl-1.5 font-semibold' : 'pl-2'}`}>{show(m(p, r.key), r.fmt)}</td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </Section>

            <Section title="Failures and recovery" right={<span className="font-mono text-[10px] uppercase text-rule">time from onset to detection and to a decision</span>}>
              {pols.every((p) => run.results[p].failures.length === 0) ? <p className="text-sm text-rule">No failures occurred with these faults.</p> : (
                <table className="w-full border-collapse text-sm">
                  <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Policy</th><th>Failure</th><th>Incident</th><th>Onset</th><th>Detected after</th><th>Decided after</th></tr></thead>
                  <tbody>
                    {pols.flatMap((p) => run.results[p].failures.map((f, i) => (
                      <tr key={`${p}-${i}`} className="border-b hair font-mono text-xs">
                        <td className="py-1"><span className="mr-1 inline-block h-2.5 w-2.5" style={{ background: POLICY_COLOR[p], border: '1px solid #BDB5A3' }} />{p}</td>
                        <td>{f.kind.replace('_', ' ')}</td><td>{f.incident}</td><td>{mmss(f.onset_s)}</td>
                        <td className={f.detected_s === null ? 'text-red' : ''}>{f.detected_s === null ? 'not detected' : `${Math.round(f.detected_s)} s`}</td>
                        <td>{f.decided_s === null ? 'n/a' : `${Math.round(f.decided_s)} s`}</td>
                      </tr>
                    )))}
                  </tbody>
                </table>
              )}
            </Section>
          </>
        )}

        {lab === 'dispatch' && bench && (
          <div className="border-t-2 border-orange">
            <div className="flex flex-wrap items-center gap-3 px-4 py-3">
              <h2 className="font-display text-3xl font-bold uppercase leading-none">Benchmark · {bench.seeds.length} seeds</h2>
              <Stamp tone="warn">simulated</Stamp>
              <span className="font-mono text-xs text-rule">seeds {bench.seeds[0]} to {bench.seeds[bench.seeds.length - 1]} · same worlds for every policy</span>
            </div>
            <div className="grid gap-px bg-rule/30 xl:grid-cols-2">
              {(['response_median_s', 'response_p90_s'] as const).map((k) => (
                <div key={k} className="bg-ink p-3">
                  <DotStrip title={`${bench.labels[k]}: every seed`} unit=" s"
                    values={Object.fromEntries(bench.policies.map((p) => [p, bench.distributions[p][k].values ?? []]))}
                    medianOf={Object.fromEntries(bench.policies.map((p) => [p, bench.distributions[p][k].median]))} />
                </div>
              ))}
            </div>
            <Section title="Distribution across seeds" right={<span className="font-mono text-[10px] uppercase text-rule">median [p10 to p90]</span>}>
              <table className="w-full border-collapse text-sm">
                <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Metric</th>{bench.policies.map((p) => <th key={p}>{POLICY_NAME[p]}</th>)}</tr></thead>
                <tbody>
                  {ROWS.map((r) => (
                    <tr key={r.key} className="border-b hair">
                      <td className="py-1.5">{r.label}</td>
                      {bench.policies.map((p) => {
                        const d = bench.distributions[p][r.key]
                        return <td key={p} className="font-mono text-xs">{d.n ? `${show(d.median, r.fmt).split(' (')[0]} [${show(d.p10, r.fmt).split(' (')[0]} to ${show(d.p90, r.fmt).split(' (')[0]}]` : 'n/a'}</td>
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
            <Section title="Where ResQGrid (P2) is worse than the baseline" right={<span className="font-mono text-[10px] uppercase text-rule">paired on identical seeds</span>}>
              {bench.where_p2_is_worse_than_p0.length === 0 ? <p className="text-sm">Nowhere on these seeds.</p> : (
                <ul>
                  {bench.where_p2_is_worse_than_p0.map((w) => (
                    <li key={w.metric} className="flex flex-wrap items-center gap-3 border-b hair py-1.5 text-sm">
                      <Stamp tone="bad">worse</Stamp><span className="min-w-[260px] font-semibold">{w.label}</span>
                      <span className="font-mono text-xs">on {w.seeds_worse} of {w.of} seeds · mean difference {w.mean_delta >= 0 ? '+' : ''}{w.mean_delta}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-2 text-xs text-rule">Deliberate hand-overs are a trade-off, not a failure: a lower-priority call waits so a P1 can be served.</p>
            </Section>
          </div>
        )}
      </section>
    </div>
  )
}
