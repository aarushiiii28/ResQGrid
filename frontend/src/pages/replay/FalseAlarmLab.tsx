import { useState } from 'react'
import { ErrorBox, Section, Stamp } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { label } from '../../lib/format'

interface Metrics {
  calls: number
  genuine: number
  accidental: number
  false_alarms_flagged: number
  false_alarms_flagged_pct: number
  missed_genuine_in_low: number
  missed_genuine_in_low_pct: number
  delayed_genuine_initial_low: number
  worst_delay_s: number
  accidental_dispatched: number
  time_to_verify_median_s: number
  time_to_verify_p90_s: number
  dispatcher_reviews: number
  dispatcher_reviews_per_100: number
}
interface Mix {
  label: string
  seed: number
  n: number
  model_version: string
  config_hash: string
  low_max: number
  low_hold_s: number
  metrics: Metrics
  scenarios: { scenario: string; genuine: boolean; n: number; low: number; uncertain: number; high: number; median_p: number }[]
  sweep: { threshold: string; value: number; false_alarms_flagged_pct: number; missed_genuine_in_low: number; missed_genuine_in_low_pct: number; dispatcher_reviews_per_100: number; current: boolean }[]
  failures: { scenario: string; p_final: number; initial_lane: string; checkin: string; top: { feature: string; contribution: number }[]; missing: string[] }[]
  delayed: { scenario: string; verify_s: number; final_lane: string }[]
  where_it_fails: string[]
}

/** M18 in the Replay Lab: a seeded synthetic call mix through the live model. The number that matters most is
 * how many genuine emergencies end up in the LOW lane, so it comes first and is not softened. */
export default function FalseAlarmLab() {
  const [seed, setSeed] = useState('42')
  const [n, setN] = useState('300')
  const [mix, setMix] = useState<Mix | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const go = async () => {
    setBusy(true)
    setErr(null)
    try {
      setMix(await api.post<Mix>('/api/simulation/false-alarm', { seed: Number(seed), n: Number(n) }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Run failed')
    } finally {
      setBusy(false)
    }
  }
  const m = mix?.metrics

  return (
    <div>
      <div className="flex flex-wrap items-end gap-3 border-b hair px-4 py-3">
        <label className="font-mono text-[10px] uppercase text-rule">
          Seed
          <input aria-label="False-alarm seed" type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} className="mt-0.5 block min-h-[40px] w-28 border border-rule/60 bg-ink px-2 text-base" />
        </label>
        <label className="font-mono text-[10px] uppercase text-rule">
          Calls
          <input aria-label="Calls in the mix" type="number" min={20} max={3000} value={n} onChange={(e) => setN(e.target.value)} className="mt-0.5 block min-h-[40px] w-28 border border-rule/60 bg-ink px-2 text-base" />
        </label>
        <button className="btn-primary" onClick={go} disabled={busy}>
          {busy ? 'Running…' : 'Run call mix'}
        </button>
        <p className="max-w-xl text-xs text-rule">
          Genuine emergencies (cardiac, crash, assault, fire, silent medical) mixed with child taps, pocket presses, test presses and abandoned calls. Invented sensor traces.
        </p>
      </div>
      {err && <div className="px-4 py-2"><ErrorBox message={err} /></div>}
      {!mix && !err && <p className="px-4 py-6 text-sm text-rule">Run the call mix to see flagged false alarms, missed genuine emergencies, time to verify and dispatcher workload.</p>}

      {mix && m && (
        <>
          <div className="flex flex-wrap items-center gap-3 px-4 py-3">
            <h2 className="font-display text-3xl font-bold uppercase leading-none">False-alarm mix · seed {mix.seed} · {mix.n} calls</h2>
            <Stamp tone="warn">simulated</Stamp>
            <Stamp tone="mute">{mix.model_version}</Stamp>
            <span className="font-mono text-xs text-rule">config {mix.config_hash} · LOW below {mix.low_max} · hold at most {mix.low_hold_s}s</span>
          </div>

          <div role="alert" className={`mx-4 mb-3 border-l-4 px-3 py-2 ${m.missed_genuine_in_low > 0 ? 'border-red bg-red/10' : 'border-green bg-green/10'}`}>
            <div className="flex flex-wrap items-baseline gap-3">
              <Stamp tone={m.missed_genuine_in_low > 0 ? 'bad' : 'ok'}>missed genuine in low lane</Stamp>
              <span className="font-display text-4xl font-bold leading-none">{m.missed_genuine_in_low} of {m.genuine}</span>
              <span className="font-mono text-sm">({m.missed_genuine_in_low_pct}% of genuine emergencies)</span>
            </div>
            <p className="mt-1 text-sm">
              These are real emergencies in this mix that the model left in the LOW lane after the check-in. They are not dispatched automatically; a dispatcher has to act. A further {m.delayed_genuine_initial_low} genuine
              calls started LOW and were held for up to {m.worst_delay_s}s before being rescued.
            </p>
          </div>

          <Section title="Results" right={<span className="font-mono text-[10px] uppercase text-rule">{m.calls} calls · {m.genuine} genuine · {m.accidental} accidental</span>}>
            <table className="w-full border-collapse text-sm">
              <tbody>
                {[
                  ['False alarms flagged (LOW)', `${m.false_alarms_flagged} of ${m.accidental}`, `${m.false_alarms_flagged_pct}% of accidental calls`],
                  ['Accidental calls still dispatched', `${m.accidental_dispatched}`, 'unknown never lowers the score, so some get through'],
                  ['Time to verify, median', `${m.time_to_verify_median_s} s`, `p90 ${m.time_to_verify_p90_s} s · check-in answer or the unanswered window`],
                  ['Dispatcher verification workload', `${m.dispatcher_reviews_per_100} per 100 calls`, `${m.dispatcher_reviews} rows in the LOW or UNCERTAIN lane`],
                ].map(([k, v, d]) => (
                  <tr key={k} className="border-b hair">
                    <td className="py-1.5 pr-3">{k}</td>
                    <td className="font-mono font-semibold">{v}</td>
                    <td className="text-xs text-rule">{d}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="By scenario" right={<span className="font-mono text-[10px] uppercase text-rule">final lane after the check-in</span>}>
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="py-1">Scenario</th><th>Truth</th><th>Calls</th><th>Low</th><th>Uncertain</th><th>High</th><th>Median P(genuine)</th>
                </tr>
              </thead>
              <tbody>
                {mix.scenarios.map((s) => (
                  <tr key={s.scenario} className={`border-b hair ${s.genuine && s.low > 0 ? 'bg-red/10' : ''}`}>
                    <td className="py-1.5">{label(s.scenario)}</td>
                    <td><Stamp tone={s.genuine ? 'bad' : 'mute'}>{s.genuine ? 'genuine' : 'accidental'}</Stamp></td>
                    <td className="font-mono">{s.n}</td>
                    <td className="font-mono">{s.low}</td>
                    <td className="font-mono">{s.uncertain}</td>
                    <td className="font-mono">{s.high}</td>
                    <td className="font-mono">{Math.round(s.median_p * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="Threshold sweep" right={<span className="font-mono text-[10px] uppercase text-rule">the trade-off</span>}>
            <table className="w-full border-collapse text-sm">
              <caption className="pb-1 text-left text-xs text-rule">
                A higher LOW boundary flags more false alarms but also leaves more genuine emergencies in LOW. A lower HIGH boundary sends more calls straight to dispatch with less dispatcher checking. The current setting is marked.
              </caption>
              <thead>
                <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                  <th className="py-1">Threshold</th><th>Value</th><th>False alarms flagged</th><th>Missed genuine in LOW</th><th>Reviews per 100</th>
                </tr>
              </thead>
              <tbody>
                {mix.sweep.map((s) => (
                  <tr key={`${s.threshold}-${s.value}`} className={`border-b hair ${s.current ? 'bg-bone/15' : ''}`}>
                    <td className="py-1.5 font-mono">{s.threshold === 'low_max' ? 'LOW below' : 'HIGH from'}</td>
                    <td className="font-mono">{s.value}</td>
                    <td className="font-mono">{s.false_alarms_flagged_pct}%</td>
                    <td className="font-mono">{s.missed_genuine_in_low} ({s.missed_genuine_in_low_pct}%)</td>
                    <td className="font-mono">{s.dispatcher_reviews_per_100}</td>
                    <td>{s.current && <Stamp tone="info">current</Stamp>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>

          <Section title="Where it fails" right={<Stamp tone="bad">read this</Stamp>}>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {mix.where_it_fails.map((w) => <li key={w}>{w}</li>)}
            </ul>
            {mix.failures.length > 0 && (
              <table className="mt-3 w-full border-collapse text-xs">
                <caption className="pb-1 text-left font-mono text-[10px] uppercase text-rule">genuine emergencies left in LOW (first {mix.failures.length})</caption>
                <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Scenario</th><th>Check-in</th><th>P(genuine)</th><th>Strongest signals</th></tr></thead>
                <tbody>
                  {mix.failures.map((f, i) => (
                    <tr key={i} className="border-b hair">
                      <td className="py-1">{label(f.scenario)}</td>
                      <td>{f.checkin}</td>
                      <td className="font-mono">{Math.round(f.p_final * 100)}%</td>
                      <td>{f.top.map((t) => `${label(t.feature)} ${t.contribution > 0 ? '+' : ''}${t.contribution}`).join(', ')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
          <p className="px-4 pb-4 font-mono text-[10px] text-rule">{mix.label}</p>
        </>
      )}
    </div>
  )
}
