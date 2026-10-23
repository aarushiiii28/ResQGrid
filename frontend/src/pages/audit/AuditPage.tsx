import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import LedgerBadge from '../../components/LedgerBadge'
import { Empty, ErrorBox, Loading, Stamp, type Tone } from '../../components/ui'
import { api, download } from '../../lib/api'
import { useLiveRefresh } from '../../lib/events'
import { ageSince, ageText, clock, label } from '../../lib/format'
import type { AuditEvent, Incident } from '../../lib/types'

const SRC_TONE: Record<string, Tone> = { human_confirmed: 'ok', measured: 'ok', reported: 'info', ai_extracted: 'warn', simulated: 'mute', system: 'mute' }
const SRC_NOTE: Record<string, string> = {
  reported: 'raw input',
  ai_extracted: 'extracted suggestion',
  human_confirmed: 'operator confirmed',
  measured: 'measured',
  simulated: 'simulated value',
}
const ENTITY_ORDER = ['incident', 'call', 'pipeline', 'false_alarm', 'dispatch', 'assignment', 'vehicle', 'prealert', 'nav', 'volunteer_alert', 'hospital', 'building', 'ledger', 'key']

function humanize(e: AuditEvent) {
  if (e.event_type === 'pipeline.step') {
    const p = e.payload as { step?: string; status?: string }
    return `Pipeline · ${label(p.step ?? 'step')} ${p.status ?? ''}`.trim()
  }
  const [a, b] = e.event_type.split('.')
  return `${label(a)} · ${label(b ?? '')}`
}

function short(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function Diff({ oldV, newV }: { oldV: unknown; newV: unknown }) {
  if (oldV == null && newV == null) return null
  const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === 'object' && !Array.isArray(x)
  if (isObj(oldV) && isObj(newV)) {
    const keys = Array.from(new Set([...Object.keys(oldV), ...Object.keys(newV)])).filter((k) => JSON.stringify(oldV[k]) !== JSON.stringify(newV[k]))
    if (keys.length === 0) return null
    return (
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 font-mono text-xs">
        {keys.map((k) => (
          <div key={k} className="contents">
            <dt className="text-rule">{label(k)}</dt>
            <dd><span className="text-red line-through">{short(oldV[k])}</span> <span aria-hidden="true">→</span> <span className="text-green">{short(newV[k])}</span></dd>
          </div>
        ))}
      </dl>
    )
  }
  return (
    <div className="mt-1 font-mono text-xs">
      <span className="text-red line-through">{short(oldV)}</span> <span aria-hidden="true">→</span> <span className="text-green">{short(newV)}</span>
    </div>
  )
}

export default function AuditPage() {
  const { incidentId } = useParams()
  const nav = useNavigate()
  const [incs, setIncs] = useState<Incident[] | null>(null)
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [exporting, setExporting] = useState<string | null>(null)

  const loadList = useCallback(async () => {
    try {
      setIncs(await api.get<Incident[]>('/api/incidents'))
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])
  useLiveRefresh(loadList, { pollMs: 8000 })

  const load = useCallback(async () => {
    if (!incidentId) return setEvents(null)
    try {
      setEvents(await api.get<AuditEvent[]>(`/api/incidents/${incidentId}/audit`))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [incidentId])
  useEffect(() => {
    setEvents(null)
    void load()
  }, [load])
  useLiveRefresh(load, { pollMs: 4000, filter: (e) => e.correlation_id === incidentId })

  const entities = useMemo(() => ENTITY_ORDER.filter((e) => events?.some((x) => x.entity_type === e)), [events])
  const shown = (events ?? []).filter((e) => !hidden.has(e.entity_type))
  const inc = incs?.find((i) => i.id === incidentId)
  const list = (incs ?? []).filter((i) => !q || i.id.toLowerCase().includes(q.toLowerCase()) || String(i.reported_category?.value ?? '').includes(q.toLowerCase()))

  const exp = async (fmt: 'json' | 'csv') => {
    if (!incidentId) return
    setExporting(fmt)
    try {
      await download(`/api/incidents/${incidentId}/audit?format=${fmt}`, `${incidentId}-audit.${fmt}`)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setExporting(null)
    }
  }

  return (
    <div className="grid min-h-full lg:grid-cols-[320px_minmax(0,1fr)]">
      <aside aria-label="Incidents" className="border-b hair lg:border-b-0 lg:border-r">
        <div className="border-b hair px-3 py-2">
          <h1 className="font-display text-3xl font-bold uppercase leading-none">Incident passport</h1>
          <p className="text-xs text-rule">One timeline per incident, append-only.</p>
          <input aria-label="Filter incidents" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by ID or reason" className="mt-2 min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-sm" />
        </div>
        {!incs ? <Loading what="Loading incidents" /> : list.length === 0 ? <div className="p-3"><Empty title="No incidents" /></div> : (
          <ul>
            {list.map((i) => (
              <li key={i.id}>
                <button onClick={() => nav(`/audit/${i.id}`)} aria-current={i.id === incidentId} className={`flex w-full items-center gap-2 border-b hair px-3 py-2 text-left ${i.id === incidentId ? 'bg-bone text-ink' : 'hover:bg-bone/10'}`}>
                  <span className="font-mono font-semibold">{i.id}</span>
                  <span className="flex-1 truncate text-sm">{i.reported_category ? label(String(i.reported_category.value)) : 'reason unknown'}</span>
                  <Stamp tone={i.status === 'closed' ? 'mute' : 'info'}>{i.status}</Stamp>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section aria-label="Timeline" className="min-w-0">
        {!incidentId ? (
          <div className="p-6"><Empty title="Choose an incident" hint="Its full history appears here, with actors, reasons and old and new values." /></div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3 border-b hair px-4 py-3">
              <h2 className="font-display text-5xl font-bold leading-none">{incidentId}</h2>
              {inc && <Stamp tone="info">{inc.status}</Stamp>}
              <span className="font-mono text-xs text-rule">{events ? `${events.length} events` : ''}</span>
              <div className="ml-auto flex gap-2">
                <button className="btn-ghost" onClick={() => exp('json')} disabled={!events || exporting !== null}>{exporting === 'json' ? 'Exporting…' : 'Export JSON'}</button>
                <button className="btn-ghost" onClick={() => exp('csv')} disabled={!events || exporting !== null}>{exporting === 'csv' ? 'Exporting…' : 'Export CSV'}</button>
              </div>
            </div>
            {err && <div className="p-3"><ErrorBox message={err} onRetry={load} /></div>}
            {!events && !err ? <Loading what="Loading timeline" /> : events && (
              <>
                <div className="flex flex-wrap items-center gap-1.5 border-b hair px-4 py-2">
                  <span className="mr-1 font-mono text-[10px] uppercase text-rule">Show</span>
                  {entities.map((e) => (
                    <button key={e} aria-pressed={!hidden.has(e)} onClick={() => { const n = new Set(hidden); n.has(e) ? n.delete(e) : n.add(e); setHidden(n) }} className={`min-h-[28px] border px-2 font-mono text-[11px] uppercase ${hidden.has(e) ? 'border-rule/40 text-rule/60 line-through' : 'border-bone bg-bone text-ink'}`}>{label(e)}</button>
                  ))}
                  <span className="ml-auto flex flex-wrap gap-1.5 font-mono text-[10px] uppercase text-rule">
                    {Object.entries(SRC_NOTE).map(([k, v]) => <span key={k} className="flex items-center gap-1"><Stamp tone={SRC_TONE[k]}>{label(k)}</Stamp>{v}</span>)}
                  </span>
                </div>
                {shown.length === 0 ? <div className="p-6"><Empty title="No events match" /></div> : (
                  <ol className="px-4 py-3">
                    {shown.map((e, i) => (
                      <li key={e.event_id} className="relative flex gap-4 pb-4">
                        <div className="flex w-[74px] shrink-0 flex-col items-end font-mono text-xs">
                          <span className="font-semibold">{clock(e.occurred_at)}</span>
                          <span className="text-[10px] text-rule">#{e.seq}</span>
                        </div>
                        <div className="relative flex flex-col items-center">
                          <span className={`mt-1 block h-3 w-3 border-2 ${e.source === 'human_confirmed' ? 'border-green bg-green' : e.source === 'simulated' ? 'border-rule bg-ink' : 'border-orange bg-orange'}`} aria-hidden="true" />
                          {i < shown.length - 1 && <span className="mt-1 w-px flex-1 bg-rule/40" aria-hidden="true" />}
                        </div>
                        <div className="min-w-0 flex-1 border-b hair pb-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-display text-2xl font-bold uppercase leading-none">{humanize(e)}</span>
                            {e.source && <Stamp tone={SRC_TONE[e.source] ?? 'mute'} title={SRC_NOTE[e.source]}>{label(e.source)}</Stamp>}
                            <span className="font-mono text-[11px] text-rule">{ageText(ageSince(e.occurred_at))} ago</span>
                          </div>
                          <div className="mt-0.5 text-sm">
                            <span className="font-mono text-xs uppercase text-rule">actor</span> <b className="font-mono">{e.actor}</b>
                            <span className="ml-3 font-mono text-xs uppercase text-rule">entity</span> <span className="font-mono">{e.entity_type}:{e.entity_id}</span>
                          </div>
                          {e.reason && <p className="mt-0.5 text-sm"><span className="font-mono text-xs uppercase text-rule">reason</span> {e.reason}</p>}
                          <Diff oldV={e.old_value} newV={e.new_value} />
                          {Object.keys(e.payload ?? {}).length > 0 && (
                            <details className="mt-1">
                              <summary className="cursor-pointer font-mono text-[11px] uppercase text-rule">details</summary>
                              <pre className="mt-1 max-h-40 overflow-auto bg-bone/5 p-2 font-mono text-[11px]">{JSON.stringify(e.payload, null, 2)}</pre>
                            </details>
                          )}
                          <div className="mt-1 font-mono text-[10px] text-rule/70">event {e.event_id.slice(0, 8)} · correlation {e.correlation_id}</div>
                          <div className="mt-1"><LedgerBadge eventId={e.event_id} ledger={e.ledger} /></div>
                        </div>
                      </li>
                    ))}
                  </ol>
                )}
              </>
            )}
          </>
        )}
      </section>
    </div>
  )
}
