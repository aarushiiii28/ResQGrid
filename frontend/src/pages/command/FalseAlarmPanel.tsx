import { useEffect, useState } from 'react'
import { Stamp, type Tone } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { ageSince, label, mmss } from '../../lib/format'
import type { FaView, Incident, Lane } from '../../lib/types'

export const LANE_TONE: Record<Lane, Tone> = { HIGH: 'bad', UNCERTAIN: 'warn', LOW: 'mute' }
export const LANE_BAR: Record<Lane, string> = { HIGH: 'bg-red', UNCERTAIN: 'bg-amber', LOW: 'bg-rule/60' }
const LANE_TEXT: Record<Lane, string> = { HIGH: 'High', UNCERTAIN: 'Uncertain', LOW: 'Low' }

export function checkInStamp(fa: FaView): { tone: Tone; text: string } | null {
  const s = fa.checkin.state
  if (s === 'none') return null
  if (s === 'prompting') return { tone: 'info', text: `check-in ${fa.checkin.prompt_n}/3` }
  if (s === 'safe') return { tone: 'ok', text: 'says safe' }
  if (s === 'unanswered') return { tone: 'bad', text: 'no answer' }
  return { tone: 'bad', text: 'asked for help' }
}

/** Compact lane + check-in stamps for the queue row. Never colour alone: the lane is always spelled out. */
export function LaneStamps({ inc, fa }: { inc: Incident; fa?: FaView }) {
  if (inc.status === 'pending') return <Stamp tone="mute">pending · countdown</Stamp>
  if (!fa) return null
  const ci = checkInStamp(fa)
  return (
    <span className="mt-0.5 flex flex-wrap items-center gap-1">
      <Stamp tone={LANE_TONE[fa.lane]}>lane {LANE_TEXT[fa.lane]}</Stamp>
      {ci && <Stamp tone={ci.tone}>{ci.text}</Stamp>}
      {fa.hold_remaining_s !== null && <Stamp tone="warn">verifying {Math.ceil(fa.hold_remaining_s)}s</Stamp>}
    </span>
  )
}

const pct = (p: number | null) => (p === null ? 'n/a' : `${Math.round(p * 100)}%`)
const fmt = (n: number) => (n > 0 ? `+${n.toFixed(2)}` : n.toFixed(2))

/** Dispatcher view of one incident's false-alarm assessment: lane, age, check-in, why, and the human actions
 * (tag outcome, close with a reason). A ruled table, not cards. */
export default function FalseAlarmPanel({ inc, fa, onChanged }: { inc: Incident; fa: FaView; onChanged: () => void }) {
  const [why, setWhy] = useState(false)
  const [mode, setMode] = useState<'none' | 'tag' | 'close'>('none')
  const [outcome, setOutcome] = useState<'accidental' | 'genuine' | 'unknown'>('accidental')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const age = ageSince(inc.created_at) ?? 0
  const ci = checkInStamp(fa)
  const terminal = ['closed', 'merged'].includes(inc.status)
  const rows = [...fa.contributions].sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    setErr(null)
    try {
      await fn()
      setMode('none')
      setReason('')
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section aria-label="False-alarm assessment" className="border-b hair">
      <div className="flex items-stretch">
        <span className={`w-1.5 shrink-0 ${LANE_BAR[fa.lane]}`} aria-hidden="true" />
        <div className="flex-1 px-3 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <h3 className="rail-title">False-alarm check</h3>
            <Stamp tone={LANE_TONE[fa.lane]}>lane {LANE_TEXT[fa.lane]}</Stamp>
            {ci && <Stamp tone={ci.tone}>{ci.text}</Stamp>}
            {inc.status === 'pending' && <Stamp tone="mute">pending · countdown running</Stamp>}
            {inc.status === 'cancelled_by_caller' && <Stamp tone="warn">cancelled by caller · kept</Stamp>}
            {fa.tag && <Stamp tone="mute">tagged {fa.tag.outcome}</Stamp>}
            {fa.close_reason && <Stamp tone="mute">closed</Stamp>}
          </div>
          <p className="mt-1 font-mono text-xs text-rule">
            P(genuine) <b className="text-bone">{pct(fa.probability_genuine)}</b> · age <b className="text-bone">{mmss(age)}</b> · {fa.place_type ?? 'place unknown'}
            {fa.corroborating.length > 0 && <> · {fa.corroborating.length} nearby report(s)</>}
          </p>
          {fa.hold_remaining_s !== null && (
            <p role="status" className="mt-1 border-l-2 border-amber pl-2 text-sm">
              Low lane: verifying for <b className="font-mono">{Math.ceil(fa.hold_remaining_s)}s</b> before dispatch. It is never dismissed.
            </p>
          )}
          {fa.recalled && (
            <p role="status" className="mt-1 border-l-2 border-amber pl-2 text-sm">
              The caller said they are safe, so the automatic assignment was recalled and the unit released. Nothing was deleted. Dispatch now if you disagree.
            </p>
          )}
          {fa.awaiting_review && (
            <p role="status" className="mt-1 border-l-2 border-amber pl-2 text-sm">
              Caller says safe. The request stays open until you close it with a reason, or dispatch.
            </p>
          )}
          {fa.checkin.state === 'unanswered' && (
            <p role="alert" className="mt-1 border-l-2 border-red pl-2 text-sm">
              No answer to the check-in. Escalated to at least uncertain. Try to reach the caller.
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5">
            <button className="btn-ghost !min-h-[34px] !px-2 !text-base" aria-expanded={why} onClick={() => setWhy(!why)}>
              Why
            </button>
            {fa.hold_remaining_s !== null && !fa.dispatch_started && (
              <button className="btn-primary !min-h-[34px] !px-2 !text-base" disabled={busy !== null} onClick={() => act('dispatch', () => api.post(`/api/incidents/${inc.id}/false-alarm/dispatch`))}>
                Dispatch now
              </button>
            )}
            <button className="btn-ghost !min-h-[34px] !px-2 !text-base" aria-pressed={mode === 'tag'} onClick={() => setMode(mode === 'tag' ? 'none' : 'tag')}>
              Tag outcome
            </button>
            {!terminal && (
              <button className="btn-danger !min-h-[34px] !px-2 !text-base" aria-pressed={mode === 'close'} onClick={() => setMode(mode === 'close' ? 'none' : 'close')}>
                Close
              </button>
            )}
          </div>
          {mode !== 'none' && (
            <form
              className="mt-2 space-y-1.5 border-l-2 border-orange pl-2"
              onSubmit={(e) => {
                e.preventDefault()
                if (mode === 'tag') void act('tag', () => api.post(`/api/incidents/${inc.id}/false-alarm/tag`, { outcome, reason }))
                else void act('close', () => api.post(`/api/incidents/${inc.id}/close`, { reason }))
              }}
            >
              {mode === 'tag' && (
                <label className="block font-mono text-[10px] uppercase text-rule">
                  Outcome
                  <select className="mt-0.5 min-h-[36px] w-full border border-rule/60 bg-ink px-1 text-sm normal-case" value={outcome} onChange={(e) => setOutcome(e.target.value as typeof outcome)}>
                    <option value="accidental">accidental</option>
                    <option value="genuine">genuine</option>
                    <option value="unknown">unknown</option>
                  </select>
                </label>
              )}
              <label className="block font-mono text-[10px] uppercase text-rule">
                Reason (required)
                <input className="mt-0.5 min-h-[36px] w-full border border-rule/60 bg-ink px-2 text-sm normal-case" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={mode === 'close' ? 'Why is it safe to close?' : 'What happened?'} />
              </label>
              <button className="btn-primary !min-h-[34px] !px-3 !text-base" type="submit" disabled={reason.trim().length < 3 || busy !== null}>
                {mode === 'close' ? 'Close with reason' : 'Save tag'}
              </button>
            </form>
          )}
          {err && <p role="alert" className="mt-1 text-sm text-red">{err}</p>}
        </div>
      </div>

      {why && (
        <div className="px-3 pb-3">
          <table className="w-full border-collapse text-xs">
            <caption className="pb-1 text-left font-mono text-[10px] uppercase text-rule">
              Why this lane · model {fa.model_version} · config {fa.config_hash}
            </caption>
            <thead>
              <tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule">
                <th className="py-1">Signal</th>
                <th>Evidence</th>
                <th>Weight</th>
                <th className="text-right">Adds</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b hair">
                <td className="py-1 font-mono">bias</td>
                <td />
                <td />
                <td className="text-right font-mono">{fa.bias !== null ? fmt(fa.bias) : ''}</td>
              </tr>
              {rows.map((c) => (
                <tr key={c.feature} className={`border-b hair ${Math.abs(c.contribution) < 1e-9 ? 'text-rule' : ''}`}>
                  <td className="py-1">
                    {label(c.feature)}
                    {c.note && <span className="block text-[10px] text-rule">{c.note}</span>}
                  </td>
                  <td className="font-mono">{c.status === 'unavailable' || c.status === 'denied' ? <span title="unknown never lowers the score">{c.status}</span> : c.status === 'not_asked' ? '-' : fmt(c.evidence)}</td>
                  <td className="font-mono">{c.weight.toFixed(1)}</td>
                  <td className="text-right font-mono">{fmt(c.contribution)}</td>
                </tr>
              ))}
              <tr className="border-t-2 border-rule/50">
                <td className="py-1 font-semibold" colSpan={3}>
                  Total (logit) → P(genuine) {pct(fa.probability_genuine)}
                </td>
                <td className="text-right font-mono font-semibold">{fa.logit !== null ? fmt(fa.logit) : ''}</td>
              </tr>
            </tbody>
          </table>
          {fa.overrides.length > 0 && <p className="mt-1 text-xs"><b>Hard override:</b> {fa.overrides.map(label).join(', ')} raised the lane above the score.</p>}
          {fa.missing.length > 0 && <p className="mt-1 text-xs text-rule">Unknown (not counted against): {fa.missing.map(label).join(', ')}.</p>}
        </div>
      )}
    </section>
  )
}
