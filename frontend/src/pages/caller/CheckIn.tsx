import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../lib/api'
import { beep, haptic, speak } from '../../lib/alerts'
import { useEvents } from '../../lib/events'

export interface CheckInView {
  incident_id: string
  status: string
  prompt: boolean
  dispatch_stopped?: boolean
  message: string
  checkin: { state: string; prompt_n: number; volume: number; reason: string; next_in_s: number | null }
}

/** Layer 4: "Are you OK? Tap if safe". Spoken and shown, repeated and louder if there is no answer.
 * No answer escalates on the server; it never dismisses the request. */
export default function CheckIn({ incidentId, active = true, onState }: { incidentId: string; active?: boolean; onState?: (v: CheckInView) => void }) {
  const [view, setView] = useState<CheckInView | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const spoken = useRef(0)
  const { subscribe } = useEvents()

  const load = useCallback(async () => {
    try {
      const v = await api.get<CheckInView>(`/api/incidents/${incidentId}/check-in`)
      setView(v)
      onState?.(v)
    } catch {
      /* the next poll retries; the SOS itself does not depend on this */
    }
  }, [incidentId, onState])

  useEffect(() => {
    if (!active) return
    void load()
    const id = setInterval(() => void load(), 2000)
    return () => clearInterval(id)
  }, [active, load])
  useEffect(() => subscribe((e) => e.correlation_id === incidentId && e.event_type.startsWith('false_alarm.checkin') && void load()), [subscribe, incidentId, load])

  const n = view?.checkin.prompt_n ?? 0
  const prompting = !!view && view.prompt && view.checkin.state === 'prompting'
  useEffect(() => {
    if (!prompting || n <= spoken.current) return
    spoken.current = n
    const vol = view!.checkin.volume || 0.5
    beep(520 + 120 * n, 220, vol)
    haptic(n > 1 ? [200, 100, 200] : 120)
    speak('Are you OK? Tap if safe.', vol)
  }, [prompting, n, view])

  const answer = async (response: 'safe' | 'help') => {
    setBusy(response)
    setErr(null)
    try {
      await api.post(`/api/incidents/${incidentId}/check-in`, { response })
      await load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not send. Try again.')
    } finally {
      setBusy(null)
    }
  }

  if (!view) return null
  if (view.checkin.state === 'safe') {
    const stopped = view.dispatch_stopped !== false && (view.dispatch_stopped || view.status === 'cancelled_by_caller')
    return (
      <div role="status" className="border-l-4 border-green bg-green/10 px-3 py-2 text-lg font-semibold">
        {view.status === 'cancelled_by_caller'
          ? 'Thanks. You told us you are safe. This request stays cancelled and on record.'
          : stopped
            ? 'You told us you are safe, so no ambulance is being sent. A dispatcher will close this request.'
            : 'You told us you are safe. Help is still on the way because your phone reported something serious. A dispatcher will confirm with you.'}
        {view.status !== 'cancelled_by_caller' && (
          <button className="mt-2 block min-h-[56px] w-full border-2 border-ink bg-ink px-4 font-display text-xl uppercase text-paper" onClick={() => answer('help')} disabled={busy !== null}>
            I need help after all
          </button>
        )}
      </div>
    )
  }
  if (view.checkin.state === 'unanswered') {
    return (
      <p role="status" className="border-l-4 border-amber bg-amber/15 px-3 py-2 text-lg font-semibold">
        We could not reach you. A dispatcher has been alerted. Tap below if you need help.
        <button className="mt-2 block min-h-[56px] w-full border-2 border-ink bg-ink px-4 font-display text-xl uppercase text-paper" onClick={() => answer('help')} disabled={busy !== null}>
          I need help
        </button>
      </p>
    )
  }
  if (!prompting) return null
  return (
    <section role="alertdialog" aria-labelledby="ci-title" className="border-4 border-ink bg-amber p-4">
      <h2 id="ci-title" className="font-display text-4xl font-bold uppercase leading-none">
        Are you OK?
      </h2>
      <p className="mt-1 text-xl font-semibold">{view.checkin.reason === 'cancelled' ? 'You cancelled an SOS. Tap if you are safe.' : 'Tap if safe. If you cannot tap, help is still coming.'}</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <button className="min-h-[88px] border-4 border-ink bg-green px-4 font-display text-4xl font-bold uppercase text-paper hover:bg-ink" onClick={() => answer('safe')} disabled={busy !== null}>
          I am safe
        </button>
        <button className="min-h-[88px] border-4 border-ink bg-red px-4 font-display text-4xl font-bold uppercase text-paper hover:bg-ink" onClick={() => answer('help')} disabled={busy !== null}>
          I need help
        </button>
      </div>
      {n > 1 && <p className="mt-2 font-mono text-xs font-semibold uppercase">Reminder {n} of 3</p>}
      {err && <p role="alert" className="mt-2 text-sm font-semibold text-red">{err}</p>}
    </section>
  )
}
