import { Fragment, useState } from 'react'
import { Stamp } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { etaRange, label } from '../../lib/format'
import type { Assignment, Candidate, Decision } from '../../lib/types'

const COMP_COLOR: Record<string, string> = {
  time: 'bg-orange',
  freshness: 'bg-amber',
  coverage_loss: 'bg-bone',
  uncertainty: 'bg-rule',
  switch: 'bg-red',
}

function ScoreBar({ c }: { c: Candidate }) {
  const w = c.components.weighted
  const total = Math.max(c.components.total, 0.001)
  return (
    <div className="flex h-2.5 w-full" role="img" aria-label={`Score components: ${Object.entries(w).map(([k, v]) => `${k} ${v.toFixed(1)}`).join(', ')}`}>
      {Object.entries(w).map(([k, v]) => (
        <span key={k} className={COMP_COLOR[k]} style={{ width: `${(v / total) * 100}%` }} title={`${label(k)} ${v.toFixed(2)}`} />
      ))}
    </div>
  )
}

export function AckLadder({ a }: { a: Assignment }) {
  const order = ['created', 'sent', 'delivered_if_known', 'acknowledged', 'accepted']
  const idx = order.indexOf(a.state)
  const bad = a.state === 'timed_out' || a.state === 'rejected'
  return (
    <div>
      <div className="flex flex-wrap gap-1" aria-label="Acknowledgment state">
        {order.map((s, i) => (
          <Stamp key={s} tone={bad ? 'mute' : i <= idx ? (i === idx ? 'info' : 'ok') : 'mute'} className={i > idx || bad ? 'opacity-60' : ''}>
            {i <= idx && !bad ? (i < idx ? '✓ ' : '') : ''}
            {label(s)}
          </Stamp>
        ))}
        {bad && <Stamp tone="bad">{label(a.state)}</Stamp>}
        {a.late && <Stamp tone="warn">late</Stamp>}
      </div>
      <p className="mt-1 text-xs text-rule">Sent is not received. Delivered is shown only when known. Accepted needs the crew.</p>
      {a.fallback && <p className="mt-1 border-l-4 border-red bg-red/10 px-2 py-1 text-xs">{a.fallback.message}</p>}
    </div>
  )
}

interface Props {
  decision: Decision
  assignment: Assignment | null
  onChanged: () => void
  onRecommend: () => void
  unitName?: (id: string) => string
  hospitalName: (id: string) => string
  busyRecommend: boolean
}

export default function DispatchCompare({ decision: d, assignment, onChanged, onRecommend, hospitalName, busyRecommend }: Props) {
  const [open, setOpen] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [pick, setPick] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [showEx, setShowEx] = useState(false)
  const pending = d.status === 'pending'
  const key = (c: Candidate) => `${c.vehicle_id}|${c.hospital_id}`
  const rec = d.recommendation
  const valid = reason.trim().length >= 3

  const act = async (action: 'approve' | 'reject' | 'override') => {
    setBusy(action)
    setErr(null)
    try {
      const sel = pick ? d.candidates.find((c) => key(c) === pick) : null
      await api.post(`/api/dispatch/${d.id}/approve`, {
        action,
        reason: reason.trim(),
        vehicle_id: action === 'override' ? sel?.vehicle_id : undefined,
        hospital_id: action === 'override' ? sel?.hospital_id : undefined,
      })
      setReason('')
      setPick(null)
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Failed')
    } finally {
      setBusy(null)
    }
  }

  const base = d.baseline
  const diff = base && rec && (base.vehicle_id !== rec.vehicle_id || base.hospital_id !== rec.hospital_id)
  const tone = d.status === 'pending' ? 'warn' : d.status === 'approved' || d.status === 'overridden' || d.status === 'kept_current' ? 'ok' : 'mute'

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b hair px-3 py-2">
        <span className="font-mono text-xs text-rule">{d.id}</span>
        <Stamp tone={tone}>{label(d.status)}</Stamp>
        <Stamp tone="mute">{d.policy_version}</Stamp>
        {d.keep_current && <Stamp tone="info">keep current plan</Stamp>}
        {d.approved_by && <Stamp tone={d.approved_by === 'demo-autopilot' ? 'warn' : 'ok'} title="Who approved">{d.approved_by === 'demo-autopilot' ? 'demo autopilot (simulated)' : d.approved_by}</Stamp>}
        <button className="btn-ghost !min-h-[30px] ml-auto !px-2 !text-sm" onClick={onRecommend} disabled={busyRecommend}>
          {busyRecommend ? 'Ranking…' : 'Re-run'}
        </button>
      </div>

      {rec && (
        <div className="border-b hair bg-bone/5 px-3 py-3">
          <div className="rail-title">Recommendation</div>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-3">
            <span className="font-display text-4xl font-bold leading-none">{rec.vehicle_id}</span>
            <span className="text-rule">to</span>
            <span className="font-display text-2xl font-bold leading-none">{hospitalName(rec.hospital_id)}</span>
          </div>
          <div className="mt-1 font-mono text-sm">
            to scene {etaRange(rec.eta_scene_low_s, rec.eta_scene_high_s)} · total {etaRange(rec.eta_total_low_s, rec.eta_total_high_s)} · score {rec.components.total.toFixed(1)}
          </div>
          <div className="mt-2 flex flex-wrap gap-1">
            {rec.reason_codes.map((r) => (
              <Stamp key={r} tone={r.includes('STALE') || r.includes('UNCERTAIN') || r.includes('DEGRADED') || r.includes('SWITCH') ? 'warn' : r.includes('KEEP') ? 'info' : 'mute'}>
                {r.replace(/_/g, ' ').toLowerCase()}
              </Stamp>
            ))}
          </div>
        </div>
      )}

      <div className="px-3 pt-3">
        <div className="mb-1 flex items-baseline justify-between">
          <h4 className="rail-title">Ranked unit / destination pairs</h4>
          <span className="font-mono text-[10px] uppercase text-rule">lower score is better</span>
        </div>
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
              <th className="w-6 py-1" aria-label="Select" />
              <th className="py-1">#</th>
              <th>Unit</th>
              <th>Hospital</th>
              <th>ETA to scene</th>
              <th className="text-right">Score</th>
            </tr>
          </thead>
          <tbody>
            {d.candidates.map((c) => {
              const k = key(c)
              const isRec = rec && key(rec) === k
              return (
                <Fragment key={k}>
                  <tr className={`cursor-pointer border-b hair hover:bg-bone/10 ${isRec ? 'bg-orange/15' : ''}`} onClick={() => setOpen(open === k ? null : k)}>
                    <td className="py-1.5">
                      {pending && (
                        <input type="radio" name="pick" aria-label={`Select ${c.vehicle_id} to ${c.hospital_id} for override`} checked={pick === k} onChange={() => setPick(k)} onClick={(e) => e.stopPropagation()} className="h-4 w-4 accent-orange" />
                      )}
                    </td>
                    <td className="font-mono">{c.rank}</td>
                    <td className="font-mono font-semibold">
                      {c.vehicle_id}
                      {c.is_current && <span className="ml-1 text-[10px] text-orange">current</span>}
                    </td>
                    <td>{hospitalName(c.hospital_id)}</td>
                    <td className="font-mono">{etaRange(c.eta_scene_low_s, c.eta_scene_high_s)}</td>
                    <td className="text-right font-mono font-semibold">{c.components.total.toFixed(1)}</td>
                  </tr>
                  <tr className="border-b hair">
                    <td />
                    <td colSpan={5} className="pb-1.5">
                      <ScoreBar c={c} />
                    </td>
                  </tr>
                  {open === k && (
                    <tr className="border-b hair bg-bone/5">
                      <td />
                      <td colSpan={5} className="py-2 pr-1">
                        <dl className="grid grid-cols-[auto_1fr_auto] gap-x-3 gap-y-0.5 font-mono text-xs">
                          {(['time', 'freshness', 'coverage_loss', 'uncertainty', 'switch'] as const).map((n) => (
                            <Fragment key={n}>
                              <dt className="flex items-center gap-1.5 uppercase text-rule">
                                <span className={`inline-block h-2 w-2 ${COMP_COLOR[n]}`} />
                                {label(n)}
                              </dt>
                              <dd className="text-right">raw {c.components[n].toFixed(2)}</dd>
                              <dd className="text-right font-semibold">{c.components.weighted[n].toFixed(2)}</dd>
                            </Fragment>
                          ))}
                        </dl>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {Object.entries(c.constraints).map(([n, ok]) => (
                            <Stamp key={n} tone={ok ? 'ok' : 'bad'}>
                              {ok ? '✓' : '✗'} {label(n)}
                            </Stamp>
                          ))}
                        </div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {c.reason_codes.map((r) => (
                            <Stamp key={r} tone="mute">
                              {r.replace(/_/g, ' ').toLowerCase()}
                            </Stamp>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
        <p className="mt-1 font-mono text-[10px] uppercase text-rule">
          Weights: time {d.weights.w_time} · freshness {d.weights.w_fresh} · coverage {d.weights.w_coverage} · uncertainty {d.weights.w_uncertainty} · switch {d.weights.w_switch}
        </p>
      </div>

      {base && (
        <div className="mx-3 mt-3 border-l-4 border-rule/60 bg-bone/5 px-3 py-2 text-sm">
          <div className="rail-title">P0 baseline for comparison</div>
          <div className="font-mono">
            {base.vehicle_id} to {hospitalName(base.hospital_id)} · to scene {etaRange(base.eta_scene_low_s, base.eta_scene_high_s)} · score {base.components.total.toFixed(1)}
          </div>
          <div className="text-xs text-rule">
            {diff ? 'P1 chose differently: it accounts for road time, data freshness, coverage and uncertainty. ' : 'P1 agrees with the baseline here. '}
            {!base.feasible && <b className="text-red">Baseline destination has an unavailable resource.</b>}
          </div>
        </div>
      )}

      <div className="px-3 pt-3">
        <button className="font-mono text-xs uppercase text-rule underline" onClick={() => setShowEx(!showEx)}>
          {showEx ? 'Hide' : 'Show'} excluded ({d.exclusions.length})
        </button>
        {showEx && (
          <ul className="mt-1 max-h-40 overflow-auto text-xs">
            {d.exclusions.map((e) => (
              <li key={e.kind + e.id} className="flex gap-2 border-b hair py-0.5">
                <span className="w-16 shrink-0 font-mono text-rule">{e.kind}</span>
                <span className="w-16 shrink-0 font-mono">{e.id}</span>
                <span>{e.reasons.map((r) => r.replace(/_/g, ' ')).join(', ')}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {assignment && (
        <div className="border-t hair px-3 py-3 mt-3">
          <div className="rail-title mb-1">Crew acknowledgment · {assignment.vehicle_id}</div>
          <AckLadder a={assignment} />
        </div>
      )}

      <div className="border-t hair px-3 py-3">
        {pending ? (
          <>
            <label className="rail-title block" htmlFor="why">Reason (required)</label>
            <input id="why" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Nearest capable unit, route clear" className="mt-1 min-h-[44px] w-full border border-rule/60 bg-ink px-3 text-sm" />
            {err && <p role="alert" className="mt-1 border-l-4 border-red bg-red/10 px-2 py-1 text-sm">{err}</p>}
            <div className="mt-2 grid grid-cols-3 gap-2">
              <button className="btn-primary" disabled={!valid || !!busy} onClick={() => act('approve')} title={valid ? '' : 'Enter a reason first'}>
                {busy === 'approve' ? '…' : d.keep_current ? 'Keep plan' : 'Approve'}
              </button>
              <button className="btn-danger" disabled={!valid || !!busy} onClick={() => act('reject')}>
                {busy === 'reject' ? '…' : 'Reject'}
              </button>
              <button className="btn-ghost" disabled={!valid || !pick || !!busy} onClick={() => act('override')} title={pick ? '' : 'Select a row above to override with'}>
                {busy === 'override' ? '…' : 'Override'}
              </button>
            </div>
            <p className="mt-1 text-xs text-rule">Override uses the row selected above. Hard constraints (availability, capability) cannot be overridden.</p>
          </>
        ) : (
          <p className="text-sm text-rule">
            {d.status === 'approved' || d.status === 'overridden' ? `Decided by ${d.approved_by}. Reason: ${d.rationale}` : d.status === 'superseded' ? 'Superseded by a newer recommendation.' : d.rationale ? `Reason: ${d.rationale}` : 'No action needed.'}
          </p>
        )}
      </div>
    </div>
  )
}
