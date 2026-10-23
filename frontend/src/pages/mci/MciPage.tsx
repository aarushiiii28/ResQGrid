import { useCallback, useEffect, useMemo, useState } from 'react'
import { Empty, ErrorBox, Loading, Section, Stamp, type Tone } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { useLiveRefresh } from '../../lib/events'
import { etaRange, label } from '../../lib/format'
import type { Incident, Mci, Patient } from '../../lib/types'

const COLORS = ['red', 'amber', 'green', 'black'] as const
const COLOR_CLASS: Record<string, string> = { red: 'bg-red text-bone', amber: 'bg-amber text-ink', green: 'bg-green text-bone', black: 'bg-bone text-ink', unassigned: 'bg-transparent text-bone' }
const COLOR_TONE: Record<string, Tone> = { red: 'bad', amber: 'warn', green: 'ok', black: 'mute', unassigned: 'mute' }

function Count({ n, c }: { n: number; c: string }) {
  return (
    <div className={`flex min-w-[88px] items-center gap-2 border ${c === 'unassigned' ? 'border-rule/60' : 'border-transparent'} ${COLOR_CLASS[c]} px-2 py-1`}>
      <span className="font-display text-3xl font-bold leading-none">{n}</span>
      <span className="font-mono text-[11px] uppercase">{c === 'unassigned' ? 'unconfirmed' : c}</span>
    </div>
  )
}

function LoadBar({ planned, enRoute, capacity, red }: { planned: number; enRoute: number; capacity: number; red: number }) {
  const cells = Math.max(capacity, planned, 1)
  return (
    <div className="flex h-5 gap-px" role="img" aria-label={`${planned} planned of ${capacity} capacity, ${enRoute} en route, ${red} red`}>
      {Array.from({ length: cells }, (_, i) => {
        const over = i >= capacity
        const mine = i < planned
        const moving = i < enRoute
        return <span key={i} className={`h-full flex-1 border ${over ? 'border-red bg-red' : mine ? (moving ? 'border-green bg-green' : 'border-orange bg-orange') : 'border-rule/50'}`} />
      })}
    </div>
  )
}

export default function MciPage() {
  const [incs, setIncs] = useState<Incident[] | null>(null)
  const [mcis, setMcis] = useState<Mci[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [newInc, setNewInc] = useState('')
  const [n, setN] = useState('8')
  const [text, setText] = useState('')
  const [reason, setReason] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [names, setNames] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    try {
      const [i, m, h] = await Promise.all([api.get<Incident[]>('/api/incidents'), api.get<Mci[]>('/api/mci'), api.get<{ id: string; name: string }[]>('/api/hospitals')])
      setIncs(i)
      setMcis(m)
      setNames(Object.fromEntries(h.map((x) => [x.id, x.name])))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])
  useLiveRefresh(load, { pollMs: 4000 })

  const cur = mcis.find((m) => m.id === sel) ?? null
  useEffect(() => {
    if (!sel && mcis.length) setSel(mcis[0].id)
  }, [mcis, sel])
  const free = useMemo(() => (incs ?? []).filter((i) => !['closed', 'merged'].includes(i.status) && !mcis.some((m) => m.incident_id === i.id)), [incs, mcis])
  useEffect(() => {
    if (!newInc && free.length) setNewInc(free[0].id)
  }, [free, newInc])

  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name)
    setErr(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Failed')
    } finally {
      setBusy(null)
    }
  }

  const counts = useMemo(() => {
    const c: Record<string, number> = { red: 0, amber: 0, green: 0, black: 0, unassigned: 0 }
    cur?.patients.forEach((p) => { c[p.confirmed ? p.triage : 'unassigned'] += 1 })
    return c
  }, [cur])

  const create = () => run('create', async () => {
    const m = await api.post<Mci>('/api/mci', { incident_id: newInc, patients: Number(n) })
    setSel(m.id)
  })
  const setTriage = (p: Patient, t: string) => cur && run(`t-${p.id}`, () => api.patch(`/api/mci/${cur.id}/patients/${p.id}`, { triage: t }))
  const suggest = () => cur && run('suggest', async () => { await api.post(`/api/mci/${cur.id}/suggest`, { text }); setText('') })
  const plan = () => cur && run('plan', async () => { await api.post(`/api/mci/${cur.id}/plan`); setNotice(null) })
  const approve = () => cur && run('approve', async () => { await api.post(`/api/mci/${cur.id}/approve`, { reason: reason.trim() }); setNotice('Approved. Units are assigned (sent, not yet accepted) and each hospital has a simulated pre-alert.'); setReason('') })

  if (!incs && !err) return <Loading what="Loading" />
  const pl = cur?.plan
  const unconfirmed = cur?.patients.filter((p) => !p.confirmed) ?? []

  return (
    <div className="grid min-h-full lg:grid-cols-[360px_minmax(0,1fr)]">
      <aside aria-label="Mass-casualty records" className="border-b hair lg:border-b-0 lg:border-r">
        <div className="border-b hair px-3 py-3">
          <h1 className="font-display text-4xl font-bold uppercase leading-none">Mass casualty</h1>
          <p className="mt-1 text-sm text-rule">People set triage colours. The planner splits patients across hospitals by capability and remaining capacity, then assigns several ambulances. Nothing moves until you approve.</p>
        </div>
        <div className="border-b hair px-3 py-3">
          <h2 className="rail-title mb-2">Open a record</h2>
          {free.length === 0 ? <p className="text-sm text-rule">No open incident without a record. Create an incident in the Command Center first.</p> : (
            <div className="space-y-2">
              <label className="block font-mono text-[10px] uppercase text-rule">Incident
                <select className="mt-0.5 min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-sm normal-case" value={newInc} onChange={(e) => setNewInc(e.target.value)}>
                  {free.map((i) => <option key={i.id} value={i.id}>{i.id} · {i.reported_category ? String(i.reported_category.value) : 'reason unknown'}</option>)}
                </select>
              </label>
              <label className="block font-mono text-[10px] uppercase text-rule">Patients
                <input type="number" min={2} max={200} className="mt-0.5 min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-sm" value={n} onChange={(e) => setN(e.target.value)} />
              </label>
              <button className="btn-primary w-full" onClick={create} disabled={busy === 'create' || !newInc || Number(n) < 2}>Open mass-casualty record</button>
            </div>
          )}
        </div>
        {err && <div className="p-3"><ErrorBox message={err} onRetry={load} /></div>}
        {mcis.length === 0 ? <div className="p-3"><Empty title="No records" /></div> : (
          <ul>
            {mcis.map((m) => (
              <li key={m.id}>
                <button aria-current={m.id === sel} onClick={() => setSel(m.id)} className={`flex w-full items-center gap-2 border-b hair px-3 py-2 text-left ${m.id === sel ? 'bg-bone text-ink' : 'hover:bg-bone/10'}`}>
                  <span className="font-mono font-semibold">{m.id}</span>
                  <span className="flex-1 text-sm">{m.incident_id} · {m.patients.length} patients</span>
                  <Stamp tone={m.status === 'approved' ? 'ok' : m.status === 'planned' ? 'info' : 'warn'}>{m.status}</Stamp>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section aria-label="Triage and plan" className="min-w-0">
        {!cur ? <div className="p-6"><Empty title="Open or choose a record" /></div> : (
          <>
            <div className="flex flex-wrap items-center gap-3 border-b hair px-4 py-3">
              <h2 className="font-display text-4xl font-bold uppercase leading-none">{cur.incident_id}</h2>
              <Stamp tone="mute">{cur.id}</Stamp>
              <Stamp tone={cur.status === 'approved' ? 'ok' : 'warn'}>{cur.status}</Stamp>
              <div className="ml-auto flex flex-wrap gap-2">
                {(['red', 'amber', 'green', 'black', 'unassigned'] as const).map((c) => <Count key={c} c={c} n={counts[c]} />)}
              </div>
            </div>

            <Section title="Triage" right={<span className="font-mono text-[10px] uppercase text-rule">colours are labels set by people</span>}>
              <div className="mb-3 flex flex-wrap items-end gap-2">
                <label className="min-w-[240px] flex-1 font-mono text-[10px] uppercase text-rule">Extract suggestions from words (not applied until you confirm)
                  <input value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. 3 red, 2 amber, 1 black" className="mt-0.5 min-h-[40px] w-full border border-rule/60 bg-ink px-2 text-sm normal-case" />
                </label>
                <button className="btn-ghost" onClick={suggest} disabled={!text.trim() || busy === 'suggest' || cur.status === 'approved'}>Suggest</button>
              </div>
              <table className="w-full border-collapse text-sm">
                <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Patient</th><th>Triage</th><th>Set by</th><th className="text-right">Set colour</th></tr></thead>
                <tbody>
                  {cur.patients.map((p) => (
                    <tr key={p.id} className="border-b hair">
                      <td className="py-1.5 font-mono font-semibold">{p.id}</td>
                      <td>
                        <Stamp tone={COLOR_TONE[p.confirmed ? p.triage : 'unassigned']}>{p.confirmed ? p.triage : 'unconfirmed'}</Stamp>
                        {!p.confirmed && p.suggested && (
                          <span className="ml-2 text-xs text-amber">suggested {p.suggested} <span className="italic text-rule">“{p.suggested_evidence}”</span></span>
                        )}
                        {!p.confirmed && <span className="ml-2 font-mono text-[10px] uppercase text-rule">planned as red</span>}
                      </td>
                      <td className="font-mono text-xs text-rule">{p.set_by ?? ''}</td>
                      <td className="text-right">
                        <span className="inline-flex gap-1">
                          {COLORS.map((c) => (
                            <button key={c} aria-label={`${p.id} ${c}`} aria-pressed={p.confirmed && p.triage === c} disabled={cur.status === 'approved' || busy === `t-${p.id}`}
                              onClick={() => setTriage(p, c)}
                              className={`min-h-[34px] min-w-[58px] border px-1.5 font-mono text-[11px] uppercase ${p.confirmed && p.triage === c ? `${COLOR_CLASS[c]} border-bone` : p.suggested === c ? 'border-amber text-amber' : 'border-rule/50 text-rule hover:border-bone hover:text-bone'} disabled:opacity-40`}>
                              {c}
                            </button>
                          ))}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {unconfirmed.length > 0 && <p className="mt-2 text-xs text-amber">{unconfirmed.length} patient(s) unconfirmed. Unknown is not minor: they are planned as red until a person sets a colour.</p>}
            </Section>

            <div className="flex flex-wrap items-center gap-3 border-b hair px-4 py-3">
              <button className="btn-primary" onClick={plan} disabled={busy === 'plan' || cur.status === 'approved'}>{busy === 'plan' ? 'Planning…' : pl ? 'Re-plan' : 'Plan split and units'}</button>
              <span className="text-xs text-rule">Planning does not commit anything.</span>
            </div>

            {pl && (
              <>
                {pl.warnings.length > 0 && (
                  <div className="border-b hair bg-amber/10 px-4 py-2">
                    {pl.warnings.map((w) => <p key={w} role="alert" className="text-sm"><Stamp tone="warn" className="mr-2">check</Stamp>{w}</p>)}
                  </div>
                )}
                <Section title="Hospital load" right={<span className="font-mono text-[10px] uppercase text-rule">orange planned · green en route · red over capacity</span>}>
                  <ul>
                    {pl.hospital_loads.filter((h) => h.planned > 0 || h.capacity > 0 || h.note).map((h) => (
                      <li key={h.hospital_id} className="border-b hair py-2">
                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                          <span className="font-semibold">{h.name} <span className="font-mono text-[11px] text-rule">{h.hospital_id}</span></span>
                          <span className="font-mono text-xs">{h.planned} of {h.capacity} · red {h.planned_red} of {h.red_capacity}{h.en_route ? ` · ${h.en_route} en route` : ''}</span>
                        </div>
                        <div className="mt-1"><LoadBar planned={h.planned} enRoute={h.en_route} capacity={h.capacity} red={h.planned_red} /></div>
                        {h.note && <p className="mt-0.5 text-xs text-amber">{h.note}</p>}
                      </li>
                    ))}
                  </ul>
                </Section>
                <Section title="Ambulance loads">
                  <table className="w-full border-collapse text-sm">
                    <thead><tr className="border-b-2 border-rule/50 text-left font-mono text-[10px] uppercase text-rule"><th className="py-1">Load</th><th>Patients</th><th>Mix</th><th>Hospital</th><th>Unit</th><th>To scene</th><th className="text-right">Status</th></tr></thead>
                    <tbody>
                      {pl.loads.map((ld) => (
                        <tr key={ld.id} className="border-b hair">
                          <td className="py-1.5 font-mono font-semibold">{ld.id}</td>
                          <td className="font-mono text-xs">{ld.patient_ids.join(' ')}</td>
                          <td>{Object.entries(ld.triage_mix).map(([k, v]) => <Stamp key={k} tone={COLOR_TONE[k]} className="mr-1">{v} {k === 'unassigned' ? 'unconf.' : k}</Stamp>)}</td>
                          <td className="text-xs">{names[ld.hospital_id] ?? ld.hospital_id}</td>
                          <td className="font-mono">{ld.vehicle_id ?? <span className="text-red">none</span>} <span className="text-[10px] text-rule">{ld.vehicle_needs.join('+')}</span></td>
                          <td className="font-mono text-xs">{ld.eta_scene_s ? etaRange(ld.eta_scene_s * 0.85, ld.eta_scene_s * 1.25) : 'n/a'}</td>
                          <td className="text-right"><Stamp tone={ld.status === 'planned' ? 'mute' : 'info'}>{label(ld.status)}</Stamp></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Section>
                <Section title="Optimiser against greedy">
                  <dl className="grid gap-x-6 gap-y-1 font-mono text-xs sm:grid-cols-2">
                    <div className="flex justify-between border-b hair py-1"><dt className="text-rule">Patient split, OR-Tools cost</dt><dd>{pl.split.ortools.cost} ({pl.split.ortools.status.toLowerCase()})</dd></div>
                    <div className="flex justify-between border-b hair py-1"><dt className="text-rule">Patient split, greedy cost</dt><dd>{pl.split.greedy.cost}</dd></div>
                    <div className="flex justify-between border-b hair py-1"><dt className="text-rule">Unit assignment, OR-Tools cost</dt><dd>{pl.assignment.ortools.cost} for {pl.assignment.ortools.assigned} loads</dd></div>
                    <div className="flex justify-between border-b hair py-1"><dt className="text-rule">Unit assignment, greedy cost</dt><dd>{pl.assignment.greedy.cost} for {pl.assignment.greedy.assigned} loads</dd></div>
                  </dl>
                  <details className="mt-2"><summary className="cursor-pointer font-mono text-[11px] uppercase text-rule">Planning rules (configuration, not clinical advice)</summary><ul className="mt-1 list-disc pl-5 text-xs text-bone/80">{pl.rules.map((r) => <li key={r}>{r}</li>)}</ul></details>
                </Section>
                {cur.status !== 'approved' ? (
                  <div className="px-4 py-4">
                    <label htmlFor="mwhy" className="rail-title block">Reason for approving (required)</label>
                    <input id="mwhy" value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 min-h-[44px] w-full max-w-xl border border-rule/60 bg-ink px-3" placeholder="e.g. Reviewed with the incident commander" />
                    <div className="mt-3 flex flex-wrap items-center gap-3">
                      <button className="btn-go" onClick={approve} disabled={reason.trim().length < 3 || busy === 'approve'}>Approve plan</button>
                      <span className="text-xs text-rule">Assigns units (sent, not accepted) and sends one simulated pre-alert per hospital.</span>
                    </div>
                  </div>
                ) : <p className="px-4 py-4 text-sm">Approved by <b>{cur.approved_by}</b>. {notice}</p>}
              </>
            )}
          </>
        )}
      </section>
    </div>
  )
}
