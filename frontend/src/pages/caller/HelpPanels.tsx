import { useCallback, useEffect, useRef, useState } from 'react'
import { Empty, ErrorBox, Loading, Stamp } from '../../components/ui'
import { api } from '../../lib/api'
import { useLiveRefresh } from '../../lib/events'
import { ageText } from '../../lib/format'
import { speak, speechSynthesisSupported, stopSpeaking } from '../../lib/speech'
import type { Aed, VolunteerAlert } from '../../lib/types'

interface FirstAidData {
  label: string
  note: string
  categories: { key: string; title: string; applies_to: string[]; rhythm_bpm?: number; steps: { text: string; say: string }[] }[]
}

const DEFAULT_FOR: Record<string, string> = { cardiac: 'cpr', unconscious: 'cpr', unknown: 'cpr', breathing: 'choking', trauma: 'bleeding' }

export function FirstAidPanel({ category }: { category: string }) {
  const [data, setData] = useState<FirstAidData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pick, setPick] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  const [step, setStep] = useState<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  useEffect(() => {
    api.get<FirstAidData>('/api/first-aid').then(setData).catch((e) => setErr(e.message))
  }, [])

  const key = pick ?? DEFAULT_FOR[category] ?? 'cpr'
  const cat = data?.categories.find((c) => c.key === key)

  const stop = useCallback(() => {
    clearTimeout(timer.current)
    stopSpeaking()
    setReading(false)
    setStep(null)
  }, [])
  useEffect(() => stop, [stop])
  useEffect(() => {
    stop()
  }, [key, stop])

  const read = () => {
    if (!cat) return
    if (reading) return stop()
    setReading(true)
    let i = 0
    const next = () => {
      if (i >= cat.steps.length) return stop()
      setStep(i)
      speak(cat.steps[i].say)
      i += 1
      timer.current = setTimeout(next, 4500)
    }
    next()
  }

  if (err) return <ErrorBox message={err} />
  if (!data) return <Loading what="Loading guidance" />

  return (
    <section aria-label="First-aid guidance" className="border-2 border-ink bg-paper">
      <div className="border-b-2 border-ink bg-amber px-3 py-2">
        <p className="font-display text-2xl font-bold uppercase leading-tight">{data.label}</p>
        <p className="text-xs text-ink/80">{data.note}</p>
      </div>
      <div role="tablist" aria-label="Guidance topics" className="grid grid-cols-3 border-b-2 border-ink">
        {data.categories.map((c) => (
          <button key={c.key} role="tab" aria-selected={c.key === key} onClick={() => setPick(c.key)} className={`min-h-[56px] border-r-2 border-ink px-2 font-display text-xl font-bold uppercase last:border-r-0 ${c.key === key ? 'bg-ink text-paper' : 'hover:bg-ink/10'}`}>
            {c.key === 'cpr' ? 'CPR' : c.key}
          </button>
        ))}
      </div>
      {cat && (
        <div className="px-3 py-3">
          <h3 className="font-display text-2xl font-bold uppercase leading-tight">{cat.title}</h3>
          {cat.rhythm_bpm && (
            <div className="my-3 flex items-center gap-4 border-2 border-ink bg-paper px-3 py-2">
              <span aria-hidden="true" className="beat block h-12 w-12 bg-red" />
              <div>
                <div className="font-display text-3xl font-bold leading-none">{cat.rhythm_bpm} per minute</div>
                <div className="text-sm">Push to the beat of this square. Hard and fast.</div>
              </div>
            </div>
          )}
          <ol className="space-y-2">
            {cat.steps.map((s, i) => (
              <li key={i} className={`flex gap-3 border-l-4 px-3 py-2 ${step === i ? 'border-orange bg-orange/15' : 'border-ink/30'}`}>
                <span className="font-display text-3xl font-bold leading-none text-orange">{i + 1}</span>
                <span className="text-[19px] font-semibold leading-snug">{s.text}</span>
              </li>
            ))}
          </ol>
          {speechSynthesisSupported() ? (
            <button onClick={read} className="mt-3 min-h-[56px] w-full border-2 border-ink px-4 font-display text-xl uppercase hover:bg-ink hover:text-paper">
              {reading ? 'Stop reading' : 'Read aloud'}
            </button>
          ) : (
            <p className="mt-3 text-sm text-ink/70">Read-aloud is not available in this browser.</p>
          )}
          <p className="mt-3 text-sm font-semibold">Follow the call-taker if they tell you something different.</p>
        </div>
      )}
    </section>
  )
}

export function AedPanel({ incidentId, aeds, category }: { incidentId: string; aeds: Aed[]; category: string }) {
  const [vols, setVols] = useState<VolunteerAlert[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setVols(await api.get<VolunteerAlert[]>(`/api/incidents/${incidentId}/volunteer-alerts`))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [incidentId])
  useLiveRefresh(load, { filter: (e) => e.correlation_id === incidentId && e.event_type.startsWith('volunteer.'), pollMs: 4000 })

  const reply = async (id: string, accept: boolean) => {
    setBusy(id)
    try {
      await api.post(`/api/volunteer-alerts/${id}/respond?accept=${accept}`)
      await load()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const tone = (s: string) => (s === 'accepted' ? 'ok' : s === 'declined' ? 'bad' : s === 'sent' ? 'warn' : 'mute')
  return (
    <section aria-label="Nearby AED and volunteers" className="border-2 border-ink bg-paper">
      <div className="border-b-2 border-ink px-3 py-2">
        <h3 className="font-display text-2xl font-bold uppercase leading-tight">Nearest AED</h3>
        <p className="text-xs text-ink/70">Synthetic AED points. Walking time is an estimate (straight line x 1.3 at 1.4 m/s).</p>
      </div>
      {aeds.length === 0 ? (
        <Empty title="No AED found nearby" hint="Stay on the line and follow the call-taker." />
      ) : (
        <ul>
          {aeds.map((a, i) => (
            <li key={a.id} className={`flex items-center gap-3 border-b border-ink/30 px-3 py-3 ${i === 0 ? 'bg-green/10' : ''}`}>
              <span aria-hidden="true" className={`grid h-9 w-9 shrink-0 place-items-center border-2 border-ink font-display text-lg font-bold ${i === 0 ? 'bg-green text-paper' : ''}`}>
                {i + 1}
              </span>
              <div className="flex-1">
                <div className="text-lg font-semibold">{a.label}{i === 0 && <span className="ml-2 font-mono text-[11px] uppercase text-green">nearest</span>}</div>
                <div className="font-mono text-sm">{a.distance_m} m · about {Math.max(1, Math.round(a.walk_low_s / 60))}-{Math.max(1, Math.round(a.walk_high_s / 60))} min walk</div>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="border-b-2 border-t-2 border-ink bg-ink px-3 py-2 text-paper">
        <h3 className="font-display text-xl font-bold uppercase leading-tight">Volunteer alerts (simulated)</h3>
        <p className="text-xs text-paper/70">Synthetic, opt-in volunteers. No message leaves this app.</p>
      </div>
      {err && <ErrorBox message={err} onRetry={load} />}
      {vols.length === 0 ? (
        <div className="px-3 py-4">
          <p className="text-lg font-semibold">{['cardiac', 'unconscious', 'breathing'].includes(category) ? 'Looking for volunteers nearby…' : 'No volunteer alert for this call.'}</p>
          <p className="text-sm text-ink/70">Alerts go out automatically for collapse and breathing calls.</p>
        </div>
      ) : (
        <ul>
          {vols.map((v) => (
            <li key={v.id} className="border-b border-ink/30 px-3 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-lg font-semibold">{v.volunteer_name}</span>
                <Stamp tone={tone(v.status)} className="!border-ink/50">{v.status.replace('_', ' ')}</Stamp>
                <span className="font-mono text-xs text-ink/70">{v.distance_m} m · {Math.max(1, Math.round(v.walk_s / 60))} min away</span>
              </div>
              {v.aed_label && <div className="text-sm">Fetch {v.aed_label}, then go to the patient.</div>}
              {v.status === 'sent' && (
                <div className="mt-2 flex gap-2">
                  <button disabled={busy === v.id} onClick={() => reply(v.id, true)} className="min-h-[56px] flex-1 border-2 border-ink bg-green px-3 font-display text-lg uppercase text-paper hover:bg-ink disabled:opacity-50">
                    Simulate: accept
                  </button>
                  <button disabled={busy === v.id} onClick={() => reply(v.id, false)} className="min-h-[56px] flex-1 border-2 border-ink px-3 font-display text-lg uppercase hover:bg-ink hover:text-paper disabled:opacity-50">
                    Simulate: decline
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="px-3 py-2 font-mono text-[11px] uppercase text-ink/60">Updated {ageText(0)} ago · simulated volunteers only</p>
    </section>
  )
}
