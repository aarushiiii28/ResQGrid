import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Empty, Kv, Loading, Section, Stamp, type Tone } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { ageSince, ageText, clock, label, valueText } from '../../lib/format'
import type { AuditEvent, Incident, Prov } from '../../lib/types'

const SRC_TONE: Record<string, Tone> = { human_confirmed: 'ok', measured: 'ok', reported: 'info', ai_extracted: 'warn', simulated: 'mute' }
const SRC_LABEL: Record<string, string> = {
  human_confirmed: 'human confirmed',
  measured: 'measured',
  reported: 'reported',
  ai_extracted: 'ai extracted',
  simulated: 'simulated',
}

function ProvRow({ k, p }: { k: string; p: Prov }) {
  return (
    <li className="border-b hair py-1.5 last:border-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-[11px] uppercase text-rule">{label(k)}</span>
        <span className="text-right font-semibold">{valueText(p.value)}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
        <Stamp tone={SRC_TONE[p.source]}>{SRC_LABEL[p.source]}</Stamp>
        <span className="font-mono text-[10px] text-rule">
          conf {Math.round(p.confidence * 100)}% · {clock(p.at)}
        </span>
        {p.evidence && <span className="text-xs italic text-bone/70">“{p.evidence}”</span>}
      </div>
    </li>
  )
}

export function IncidentDetail({ inc, onChanged }: { inc: Incident; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const fields: [string, Prov][] = []
  if (inc.reported_category) fields.push(['reported_category', inc.reported_category])
  if (inc.operator_priority) fields.push(['operator_priority', inc.operator_priority])
  if (inc.patient_count) fields.push(['patient_count', inc.patient_count])
  for (const [k, v] of Object.entries(inc.facts)) fields.push([k, v])
  const confirmed = fields.filter(([, p]) => p.human_confirmed || p.source === 'measured')
  const reported = fields.filter(([, p]) => !(p.human_confirmed || p.source === 'measured'))
  const loc = inc.location_candidates[0]
  const unresolved = inc.conflicts.filter((c) => !c.resolved)

  const resolve = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.post(`/api/incidents/${inc.id}/review/resolve`)
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      {(inc.needs_review || unresolved.length > 0) && (
        <div className="border-b hair bg-amber/10 px-3 py-2">
          <div className="flex items-center gap-2">
            <Stamp tone="warn">needs human review</Stamp>
            <span className="text-xs text-rule">{inc.review_reasons.join(', ').replace(/_/g, ' ') || 'conflicting details'}</span>
          </div>
          {unresolved.map((c, i) => (
            <p key={i} className="mt-1 text-sm">
              <b>{label(c.field)}</b>: kept <span className="font-mono">{valueText(c.existing)}</span>, caller/extractor said <span className="font-mono">{valueText(c.incoming)}</span>. Not overwritten.
            </p>
          ))}
          <button className="btn-ghost mt-2 !min-h-[34px] !text-base" onClick={resolve} disabled={busy}>
            {busy ? 'Saving…' : 'Mark reviewed'}
          </button>
          {err && <p role="alert" className="mt-1 text-sm text-red">{err}</p>}
        </div>
      )}
      <Section title="Reported" right={<span className="font-mono text-[10px] uppercase text-rule">not yet confirmed</span>}>
        {reported.length === 0 ? <p className="text-sm text-rule">Nothing reported yet.</p> : <ul>{reported.map(([k, p]) => <ProvRow key={k} k={k} p={p} />)}</ul>}
      </Section>
      <Section title="Confirmed" right={<span className="font-mono text-[10px] uppercase text-rule">human or measured</span>}>
        {confirmed.length === 0 ? <p className="text-sm text-rule">Nothing confirmed yet.</p> : <ul>{confirmed.map(([k, p]) => <ProvRow key={k} k={k} p={p} />)}</ul>}
      </Section>
      <Section title="Unknown" right={<span className="font-mono text-[10px] uppercase text-rule">unknown is not available</span>}>
        {inc.unknowns.length === 0 ? <p className="text-sm text-green">No open unknowns.</p> : (
          <div className="flex flex-wrap gap-1.5">
            {inc.unknowns.map((u) => <Stamp key={u} tone="warn">? {label(u)}</Stamp>)}
          </div>
        )}
      </Section>
      <Section title="Location">
        {loc ? (
          <dl>
            <Kv k="position"><span className="font-mono">{loc.lat.toFixed(5)}, {loc.lon.toFixed(5)}</span></Kv>
            <Kv k="accuracy">{loc.accuracy_m ? `±${Math.round(loc.accuracy_m)} m` : 'unknown'} {loc.approximate && <Stamp tone="warn">approximate</Stamp>}</Kv>
            <Kv k="source">{loc.label ?? loc.source}</Kv>
            {inc.building_id && <Kv k="building"><span className="font-mono">{inc.building_id}</span></Kv>}
          </dl>
        ) : <p className="text-sm text-rule">No location yet.</p>}
      </Section>
      {inc.duplicate_suggestions.filter((s) => s.status === 'suggested').length > 0 && (
        <Section title="Possible duplicates">
          {inc.duplicate_suggestions.filter((s) => s.status === 'suggested').map((s) => (
            <p key={s.incident_id} className="text-sm"><span className="font-mono">{s.incident_id}</span> · score {s.score} · {s.reasons.join(', ')}. <span className="text-rule">Suggested only, never auto-merged.</span></p>
          ))}
        </Section>
      )}
    </div>
  )
}

export function TimelineList({ incidentId }: { incidentId: string }) {
  const [ev, setEv] = useState<AuditEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    const load = () => api.get<AuditEvent[]>(`/api/incidents/${incidentId}/audit`).then((r) => alive && setEv(r)).catch((e) => alive && setErr(e.message))
    load()
    const t = setInterval(load, 4000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [incidentId])
  if (err) return <p role="alert" className="px-3 py-2 text-sm text-red">{err}</p>
  if (!ev) return <Loading what="Loading timeline" />
  if (ev.length === 0) return <Empty title="No events" />
  return (
    <div>
      <div className="flex items-center justify-between px-3 py-2">
        <span className="rail-title">Timeline · {ev.length} events</span>
        <Link to={`/audit/${incidentId}`} className="font-mono text-xs uppercase text-orange underline">Open passport</Link>
      </div>
      <ol className="px-3 pb-3">
        {[...ev].reverse().map((e) => (
          <li key={e.event_id} className="border-b hair py-1.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-mono text-xs font-semibold">{e.event_type}</span>
              <span className="font-mono text-[10px] text-rule">{clock(e.occurred_at)} · {ageText(ageSince(e.occurred_at))} ago</span>
            </div>
            <div className="text-xs text-rule">
              {e.actor}
              {e.source ? ` · ${e.source}` : ''}
              {e.reason ? ` · ${e.reason}` : ''}
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}
