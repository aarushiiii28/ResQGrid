import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Empty, ErrorBox, Loading, Section, Stamp, type Tone } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { useLiveRefresh } from '../../lib/events'
import { ageSince, ageText, clock, label, valueText } from '../../lib/format'
import type { RecoveryOption, Risk } from '../../lib/types'

const SEV_BAR: Record<string, string> = { critical: 'bg-red', high: 'bg-orange', medium: 'bg-amber', low: 'bg-green' }
const SEV_TONE: Record<string, Tone> = { critical: 'bad', high: 'info', medium: 'warn', low: 'ok' }
const STATUS_TONE: Record<string, Tone> = { open: 'warn', awaiting: 'info', resolved: 'ok', cleared: 'mute' }

function minutes(s: number | null) {
  if (s === null) return 'unknown'
  const m = s / 60
  return `${m > 0 ? '+' : ''}${Math.abs(m) < 0.05 ? '0' : m.toFixed(1)} min`
}

function OptionRow({ o, selected, onSelect, disabled }: { o: RecoveryOption; selected: boolean; onSelect: () => void; disabled: boolean }) {
  const e = o.effects
  return (
    <li className={`border-b hair ${selected ? 'bg-orange/10' : ''}`}>
      <label className="flex cursor-pointer gap-3 px-3 py-3">
        <input type="radio" name="option" className="mt-1.5 h-5 w-5 accent-orange" checked={selected} onChange={onSelect} disabled={disabled} aria-label={o.label} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-2xl font-bold uppercase leading-none">{o.label}</span>
            {o.recommended && <Stamp tone="ok">recommended</Stamp>}
            {o.necessary && <Stamp tone="bad">current plan unsafe</Stamp>}
            {o.below_threshold && <Stamp tone="mute" title="The benefit is smaller than the minimum needed to change a plan">below benefit threshold</Stamp>}
            {!o.changes_plan && o.kind !== 'keep_current_plan' && <Stamp tone="mute">no plan change</Stamp>}
            {o.changes_plan && <Stamp tone="warn">changes the plan</Stamp>}
          </div>
          <p className="mt-0.5 text-sm text-bone/80">{o.description}</p>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs sm:grid-cols-4">
            <div><dt className="uppercase text-rule">Time</dt><dd className="text-sm">{minutes(e.time_delta_s)}</dd></div>
            <div><dt className="uppercase text-rule">Suitability</dt><dd><Stamp tone={e.suitability === 'improved' ? 'ok' : e.suitability === 'unverified' ? 'warn' : e.suitability === 'reduced' ? 'bad' : 'mute'}>{e.suitability}</Stamp></dd></div>
            <div><dt className="uppercase text-rule">Coverage</dt><dd className="text-sm">{e.coverage_delta === null ? 'unknown' : `${e.coverage_delta >= 0 ? '+' : ''}${(e.coverage_delta * 100).toFixed(1)} pts`}</dd></div>
            <div><dt className="uppercase text-rule">Benefit</dt><dd className="text-sm">{o.benefit === null ? 'n/a' : `${o.benefit.toFixed(1)} pts`}</dd></div>
          </dl>
          {e.suitability_note && <p className="mt-1 text-xs text-rule">{e.suitability_note}. {e.uncertainty}</p>}
          {e.other_incidents.length > 0 && (
            <ul className="mt-1 border-l-2 border-amber pl-2 text-xs">
              {e.other_incidents.map((x) => <li key={x.incident_id}><b className="font-mono">{x.incident_id}</b>: {x.impact}</li>)}
            </ul>
          )}
          {o.trade_offs.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-bone/70">
              {o.trade_offs.map((t) => <li key={t}>{t}</li>)}
            </ul>
          )}
        </div>
      </label>
    </li>
  )
}

export default function RiskPage() {
  const [risks, setRisks] = useState<Risk[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [pick, setPick] = useState<string>('keep_current_plan')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [autopilot, setAutopilot] = useState<boolean | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [r, a] = await Promise.all([api.get<Risk[]>('/api/risks'), api.get<{ enabled: boolean }>('/api/dispatch/autopilot')])
      setRisks(r)
      setAutopilot(a.enabled)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])
  useLiveRefresh(load, { pollMs: 4000 })

  const shown = useMemo(() => (risks ?? []).filter((r) => showAll || r.status === 'open' || r.status === 'awaiting'), [risks, showAll])
  useEffect(() => {
    if (!sel && shown.length) setSel(shown[0].id)
  }, [shown, sel])
  const cur = risks?.find((r) => r.id === sel) ?? null
  useEffect(() => {
    if (cur) setPick(cur.options.find((o) => o.recommended)?.kind ?? 'keep_current_plan')
    setReason('')
    setNotice(null)
  }, [cur?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    setErr(null)
    try {
      await fn()
      await load()
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Failed'
      setErr(msg)
      if (e instanceof ApiError && e.status === 409) await load()
    } finally {
      setBusy(null)
    }
  }

  const evaluate = () => run('eval', async () => {
    const r = await api.post<Risk[]>('/api/risks/evaluate')
    setNotice(r.length ? `${r.length} new risk${r.length === 1 ? '' : 's'} found.` : 'No new risks. Existing ones are deduplicated, not re-raised.')
  })
  const approve = () => cur && run('approve', async () => {
    const r = await api.post<Risk>(`/api/risks/${cur.id}/recover`, { option_kind: pick, reason: reason.trim() })
    setNotice(r.status === 'awaiting' ? 'Approved. Waiting for the driver or hospital to act.' : r.escalated ? 'Escalated (simulated). Nothing changed automatically.' : 'Approved and applied. See the passport for the audit trail.')
    setReason('')
  })

  if (!risks && !err) return <Loading what="Loading risks" />
  const open = (risks ?? []).filter((r) => r.status === 'open' || r.status === 'awaiting')

  return (
    <div className="grid min-h-full lg:grid-cols-[400px_minmax(0,1fr)]">
      <aside aria-label="Risks" className="border-b hair lg:border-b-0 lg:border-r">
        <div className="border-b hair px-3 py-3">
          <h1 className="font-display text-4xl font-bold uppercase leading-none">Risk and recovery</h1>
          <p className="mt-1 text-sm text-rule">Eight triggers watched on live plans. Every proposal needs approval. Keep current plan is always an option.</p>
          <div className="mt-3 flex items-center gap-2">
            <button className="btn-primary" onClick={evaluate} disabled={busy === 'eval'}>{busy === 'eval' ? 'Checking…' : 'Evaluate now'}</button>
            <span className="font-mono text-xs text-rule">auto-check every 5 s</span>
          </div>
          {notice && <p role="status" className="mt-2 border-l-4 border-orange bg-orange/10 px-2 py-1 text-sm">{notice}</p>}
          {autopilot !== null && (
            <p className="mt-2 text-xs text-rule">
              Demo Autopilot is <b>{autopilot ? 'on' : 'off'}</b>.{' '}
              {autopilot ? 'It may approve a recommended action (audited as demo-autopilot). It leaves keep-plan and escalate to a human.' : 'Every approval is yours.'}
            </p>
          )}
        </div>
        <div className="flex items-center justify-between border-b hair px-3 py-2">
          <span className="rail-title">{open.length} open</span>
          <label className="flex items-center gap-2 font-mono text-[11px] uppercase text-rule">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} className="accent-orange" /> show resolved
          </label>
        </div>
        {err && <div className="p-3"><ErrorBox message={err} onRetry={load} /></div>}
        {shown.length === 0 ? (
          <div className="p-3"><Empty title="No open risks" hint="Plans are holding. Close a road, age a vehicle's GPS, or mark a hospital resource unavailable to see one." /></div>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="w-1" /><th className="py-1 pl-2">Risk</th><th>Incident</th><th>Age</th><th className="pr-2 text-right">State</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} onClick={() => setSel(r.id)} className={`cursor-pointer border-b hair ${r.id === sel ? 'bg-bone/15' : 'hover:bg-bone/5'}`}>
                  <td className={`w-1 p-0 ${SEV_BAR[r.severity]}`}><span className="sr-only">{r.severity}</span></td>
                  <td className="py-2 pl-2">
                    <button className="text-left" aria-pressed={r.id === sel} aria-label={`Open risk ${r.id}`}>
                      <span className="block font-semibold capitalize leading-tight">{label(r.type)}</span>
                      <span className="block font-mono text-[10px] text-rule">{r.id} · {r.entity}{r.seen_count > 1 ? ` · seen ${r.seen_count}x` : ''}</span>
                    </button>
                  </td>
                  <td className="font-mono text-xs">{r.incident_id}</td>
                  <td className="font-mono text-xs text-rule">{ageText(ageSince(r.detected_at))}</td>
                  <td className="pr-2 text-right"><Stamp tone={STATUS_TONE[r.status]}>{r.status}</Stamp></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </aside>

      <section aria-label="Risk detail" className="min-w-0">
        {!cur ? (
          <div className="p-6"><Empty title="Choose a risk" hint="Evidence, options and trade-offs appear here." /></div>
        ) : (
          <>
            <div className="flex items-stretch border-b hair">
              <span className={`w-1.5 ${SEV_BAR[cur.severity]}`} aria-hidden="true" />
              <div className="flex-1 px-4 py-3">
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="font-display text-4xl font-bold uppercase leading-none">{label(cur.type)}</h2>
                  <Stamp tone={SEV_TONE[cur.severity]}>{cur.severity}</Stamp>
                  <Stamp tone={STATUS_TONE[cur.status]}>{cur.status}</Stamp>
                  <Link to={`/audit/${cur.incident_id}`} className="font-mono text-xs uppercase text-orange underline">{cur.incident_id} passport</Link>
                </div>
                <p className="mt-2 text-lg"><span className="font-mono text-xs uppercase text-rule">Affected assumption </span><b>{cur.affected_assumption}.</b></p>
                <p className="font-mono text-xs text-rule">Detected {clock(cur.detected_at)} · last seen {ageText(ageSince(cur.last_seen_at))} ago · {cur.id}</p>
              </div>
            </div>

            <Section title="Evidence" right={<span className="font-mono text-[10px] uppercase text-rule">what the monitor observed</span>}>
              <table className="w-full border-collapse text-sm">
                <tbody>
                  {cur.evidence.map((e) => (
                    <tr key={e.key} className="border-b hair last:border-0">
                      <td className="w-56 py-1 font-mono text-[11px] uppercase text-rule">{label(e.key)}</td>
                      <td className="py-1 font-mono">{Array.isArray(e.value) ? e.value.map(valueText).join(', ') : valueText(e.value)}</td>
                      <td className="py-1 text-right font-mono text-[10px] text-rule">{e.at ? clock(e.at) : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>

            <Section title="Options" right={<span className="font-mono text-[10px] uppercase text-rule">time, suitability, coverage, other incidents</span>}>
              <ul className="-mx-3 border-t hair">
                {cur.options.map((o) => (
                  <OptionRow key={o.id} o={o} selected={pick === o.kind} onSelect={() => setPick(o.kind)} disabled={cur.status === 'resolved' || cur.status === 'cleared'} />
                ))}
              </ul>
            </Section>

            {(cur.status === 'open' || cur.status === 'awaiting') ? (
              <div className="border-b hair px-4 py-4">
                <label htmlFor="why" className="rail-title block">Reason (required)</label>
                <input id="why" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this option" className="mt-1 min-h-[44px] w-full max-w-xl border border-rule/60 bg-ink px-3" />
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button className="btn-primary" onClick={approve} disabled={reason.trim().length < 3 || busy === 'approve'} title={reason.trim().length < 3 ? 'Enter a reason first' : ''}>
                    {busy === 'approve' ? 'Applying…' : `Approve: ${label(pick)}`}
                  </button>
                  <span className="text-xs text-rule">Plan changes happen only here, with your name on them. Nothing is cancelled silently.</span>
                </div>
              </div>
            ) : (
              <div className="px-4 py-4 text-sm">
                <span className="rail-title">Outcome</span>
                <p className="mt-1">{cur.resolution?.option ? `${label(cur.resolution.option)} by ${cur.resolution.by}${cur.resolution.reason ? `: ${cur.resolution.reason}` : ''}` : cur.resolution?.reason ?? 'The condition no longer holds.'}</p>
              </div>
            )}
            {cur.confirmations.length > 0 && (
              <Section title="Confirmation requests (simulated)">
                <ul className="text-sm">{cur.confirmations.map((c) => <li key={c.id} className="border-b hair py-1"><span className="font-mono">{c.id}</span> to {c.target_kind} <b>{c.target_id}</b>{c.resource ? ` (${c.resource})` : ''} at {clock(c.requested_at)}. No real contact.</li>)}</ul>
              </Section>
            )}
          </>
        )}
      </section>
    </div>
  )
}
