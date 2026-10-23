import { useState } from 'react'
import { ErrorBox, Section, Stamp } from '../../components/ui'
import { api, ApiError } from '../../lib/api'

interface Stat {
  mean: number | null
  median: number | null
  n: number
}
interface Result {
  label: string
  seeds: number[]
  policy_version: string
  names: Record<string, string>
  metrics: { key: string; label: string }[]
  summary: Record<string, Record<string, Stat>>
  per_seed: { seed: number; has_wrong_turn: boolean; has_closure: boolean; congestion_zones: number; policies: Record<string, Record<string, number | null>> }[]
  where_stable_is_worse: { vs: string; vs_name: string; metric: string; label: string; seeds_worse: number; of: number; mean_delta: number }[]
  sweep: { setting: string; value: number; current: boolean; mean_reroutes: number; mean_total_time_s: number; mean_eta_std_s: number }[]
  thresholds: { congestion: Record<string, number>; off_route: Record<string, number>; rejoin: Record<string, number> }
}

const LOWER_IS_BETTER = true
const fmt = (v: number | null | undefined) => (v === null || v === undefined ? 'n/a' : String(v))

/** M20 in the Replay Lab: the same seeded trips under three route policies. Lower is better on every metric. The places where the
 * stable policy is WORSE come with the numbers, not hidden. */
export default function RouteLab() {
  const [seed, setSeed] = useState('1')
  const [n, setN] = useState('8')
  const [res, setRes] = useState<Result | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const go = async () => {
    setBusy(true)
    setErr(null)
    try {
      setRes(await api.post<Result>('/api/simulation/routes', { seed: Number(seed), n: Number(n), sweep: true }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Run failed')
    } finally {
      setBusy(false)
    }
  }

  const best = (key: string) => {
    if (!res) return null
    const vals = Object.entries(res.summary).map(([p, m]) => [p, m[key]?.mean] as const).filter(([, v]) => v !== null && v !== undefined) as [string, number][]
    if (vals.length < 2) return null
    const b = LOWER_IS_BETTER ? Math.min(...vals.map((v) => v[1])) : Math.max(...vals.map((v) => v[1]))
    return vals.filter((v) => v[1] === b).map((v) => v[0])
  }

  return (
    <div>
      <div className="flex flex-wrap items-end gap-3 border-b hair px-4 py-3">
        <label className="font-mono text-[10px] uppercase text-rule">
          First seed
          <input aria-label="Route seed" type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} className="mt-0.5 block min-h-[40px] w-28 border border-rule/60 bg-ink px-2 text-base" />
        </label>
        <label className="font-mono text-[10px] uppercase text-rule">
          Trips
          <input aria-label="Number of trips" type="number" min={2} max={24} value={n} onChange={(e) => setN(e.target.value)} className="mt-0.5 block min-h-[40px] w-28 border border-rule/60 bg-ink px-2 text-base" />
        </label>
        <button className="btn-primary" onClick={go} disabled={busy}>
          {busy ? 'Running…' : 'Run route comparison'}
        </button>
        <p className="max-w-xl text-xs text-rule">
          The same trips with invented, changing congestion, wrong turns and closures. A: reroute on every change. B: never reroute. C: the stable policy, which plans once, rejoins first and reroutes only when worth it.
        </p>
      </div>
      {err && <div className="px-4 py-2"><ErrorBox message={err} /></div>}
      {!res && !err && <p className="px-4 py-6 text-sm text-rule">Run the comparison to see route changes, total time, ETA stability, turns changed and time lost after wrong turns.</p>}

      {res && (
        <>
          <div className="flex flex-wrap items-center gap-3 px-4 py-3">
            <h2 className="font-display text-3xl font-bold uppercase leading-none">Route policies · {res.seeds.length} trips</h2>
            <Stamp tone="warn">simulated</Stamp>
            <Stamp tone="mute">{res.policy_version}</Stamp>
            <span className="font-mono text-xs text-rule">seeds {res.seeds[0]} to {res.seeds[res.seeds.length - 1]} · same trips for every policy · lower is better</span>
          </div>

          <Section title="Results" right={<span className="font-mono text-[10px] uppercase text-rule">mean across trips</span>}>
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="py-1">Metric</th>
                  {Object.keys(res.summary).map((p) => <th key={p}>{p} · {res.names[p]}</th>)}
                </tr>
              </thead>
              <tbody>
                {res.metrics.map((m) => {
                  const b = best(m.key)
                  return (
                    <tr key={m.key} className="border-b hair">
                      <td className="py-1.5">{m.label}</td>
                      {Object.keys(res.summary).map((p) => (
                        <td key={p} className={`font-mono ${b?.includes(p) ? 'border-l-4 border-green pl-2' : 'pl-3'}`}>
                          {fmt(res.summary[p][m.key]?.mean)} {b?.includes(p) && <span className="ml-1 font-mono text-[10px] uppercase text-green">best</span>}
                        </td>
                      ))}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Section>

          <Section title="Where the stable policy is worse" right={<Stamp tone="bad">read this</Stamp>}>
            {res.where_stable_is_worse.length === 0 ? (
              <p className="text-sm">Nowhere on these trips.</p>
            ) : (
              <ul>
                {res.where_stable_is_worse.map((w) => (
                  <li key={`${w.vs}-${w.metric}`} className="flex flex-wrap items-center gap-3 border-b hair py-1.5 text-sm">
                    <Stamp tone="bad">worse than {w.vs}</Stamp>
                    <span className="min-w-[220px] font-semibold">{w.label}</span>
                    <span className="font-mono text-xs">on {w.seeds_worse} of {w.of} trips · mean difference +{w.mean_delta} · versus {w.vs_name.toLowerCase()}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-xs text-rule">A stable policy trades a little speed in some trips for fewer and calmer changes. Against never-reroute it changes the route more often because it reacts to wrong turns and closed roads.</p>
          </Section>

          <Section title="Threshold sweep" right={<span className="font-mono text-[10px] uppercase text-rule">stable policy only</span>}>
            <table className="w-full border-collapse text-sm">
              <caption className="pb-1 text-left text-xs text-rule">
                What changes when a threshold moves. The current setting is marked. In this toy model the results are not very sensitive to these two settings, which is itself a finding.
              </caption>
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="py-1">Setting</th><th>Value</th><th>Route changes</th><th>Total time (s)</th><th>ETA variability (s)</th><th />
                </tr>
              </thead>
              <tbody>
                {res.sweep.map((s) => (
                  <tr key={`${s.setting}-${s.value}`} className={`border-b hair ${s.current ? 'bg-bone/15' : ''}`}>
                    <td className="py-1.5 font-mono">{s.setting.replace(/_/g, ' ')}</td>
                    <td className="font-mono">{s.value}</td>
                    <td className="font-mono">{s.mean_reroutes}</td>
                    <td className="font-mono">{s.mean_total_time_s}</td>
                    <td className="font-mono">{s.mean_eta_std_s}</td>
                    <td>{s.current && <Stamp tone="info">current</Stamp>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="Per trip" right={<span className="font-mono text-[10px] uppercase text-rule">total time (s) · route changes</span>}>
            <details>
              <summary className="cursor-pointer font-mono text-xs uppercase text-rule">Show every trip</summary>
              <table className="mt-2 w-full border-collapse text-xs">
                <thead>
                  <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                    <th className="py-1">Seed</th><th>Events</th>{Object.keys(res.summary).map((p) => <th key={p}>{p}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {res.per_seed.map((r) => (
                    <tr key={r.seed} className="border-b hair">
                      <td className="py-1 font-mono">{r.seed}</td>
                      <td>{r.congestion_zones} congestion{r.has_closure ? ', closure' : ''}{r.has_wrong_turn ? ', wrong turn' : ''}</td>
                      {Object.keys(res.summary).map((p) => <td key={p} className="font-mono">{fmt(r.policies[p].total_time_s)} · {fmt(r.policies[p].reroutes)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </Section>
          <p className="px-4 pb-4 font-mono text-[10px] text-rule">{res.label}</p>
        </>
      )}
    </div>
  )
}
