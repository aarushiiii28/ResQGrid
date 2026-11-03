import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../lib/api'
import { mmss } from '../../lib/format'
import { listenOnce, speak, speechRecognitionSupported, speechSynthesisSupported, stopSpeaking } from '../../lib/speech'
import type { TranscriptLine } from '../../lib/types'
import { Stamp } from '../../components/ui'
import type { Session } from './useSos'

interface UtterResp {
  transcript: TranscriptLine[]
  operator: string | null
  questions_asked: number
  finished: boolean
}

const QUICK: Record<'what' | 'state' | 'count', string[]> = {
  what: ['Chest pain', 'Not breathing', 'Car or bike accident', 'Bleeding badly', 'Choking', 'Someone collapsed', 'Newborn baby', 'Face drooping'],
  state: ['Awake and breathing', 'Not responding and not breathing', 'Not responding but breathing', 'Awake but struggling to breathe'],
  count: ['One person', 'Two people', 'Three people', 'Four people'],
}

const SCRIPT: Record<'what' | 'state' | 'count', string> = {
  what: 'My father collapsed, I think it is a heart attack, he has chest pain',
  state: 'He is not responding and not breathing',
  count: 'Just one person',
}

function kind(q: string | null): 'what' | 'state' | 'count' | null {
  if (!q) return null
  if (/what happened/i.test(q)) return 'what'
  if (/awake|breathing/i.test(q)) return 'state'
  if (/how many/i.test(q)) return 'count'
  return null
}

const SAFE_AUTO_END_MS = 10_000

/** `autoEnd`: the caller said they are safe. If nothing else happens for 10 s, the call ends. */
export default function CallPanel({ session, onEnded, autoEnd = false }: { session: Session; onEnded: () => void; autoEnd?: boolean }) {
  const [lines, setLines] = useState<TranscriptLine[]>([
    { speaker: 'operator', text: session.operator, at: new Date().toISOString(), mode: 'scripted' },
  ])
  const [secs, setSecs] = useState(Math.floor((Date.now() - session.started_at) / 1000))
  const [muted, setMuted] = useState(false)
  const [listening, setListening] = useState(false)
  const [interim, setInterim] = useState('')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [scripted, setScripted] = useState(false)
  const [ended, setEnded] = useState(false)
  const [micNote, setMicNote] = useState<string | null>(null)
  const stopRef = useRef<(() => void) | null>(null)
  const spokenRef = useRef(0)
  const logRef = useRef<HTMLDivElement>(null)

  const lastOperator = [...lines].reverse().find((l) => l.speaker === 'operator')?.text ?? null
  const q = kind(lastOperator)
  const finished = !!lastOperator && /^help is on the way/i.test(lastOperator)
  const canMic = speechRecognitionSupported()

  useEffect(() => {
    const t = setInterval(() => setSecs(Math.floor((Date.now() - session.started_at) / 1000)), 1000)
    return () => clearInterval(t)
  }, [session.started_at])

  // operator lines are read aloud (speech synthesis) unless muted; typed/tap replies work either way
  useEffect(() => {
    const ops = lines.filter((l) => l.speaker === 'operator')
    if (ops.length > spokenRef.current) {
      const latest = ops[ops.length - 1].text
      spokenRef.current = ops.length
      if (!muted && speechSynthesisSupported()) speak(latest)
    }
  }, [lines, muted])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [lines, interim])

  useEffect(() => () => {
    stopRef.current?.()
    stopSpeaking()
  }, [])

  const send = useCallback(
    async (text: string, mode: 'speech' | 'typed' | 'quick' | 'scripted') => {
      const clean = text.trim()
      if (!clean || busy || ended) return
      setBusy(true)
      setErr(null)
      setLines((l) => [...l, { speaker: 'caller', text: clean, at: new Date().toISOString(), mode }])
      try {
        const r = await api.post<UtterResp>(`/api/calls/${session.call_id}/utterances`, { text: clean, mode })
        setLines(r.transcript)
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : 'Could not send. Try again.')
      } finally {
        setBusy(false)
        setTyped('')
        setInterim('')
      }
    },
    [busy, ended, session.call_id],
  )

  // scripted demo: answers the operator for you, so the whole flow can be shown hands-free
  useEffect(() => {
    if (!scripted || busy || finished || ended || !q) return
    const t = setTimeout(() => void send(SCRIPT[q], 'scripted'), 1800)
    return () => clearTimeout(t)
  }, [scripted, busy, finished, ended, q, lines.length, send])

  const mic = () => {
    if (listening) {
      stopRef.current?.()
      return
    }
    setMicNote(null)
    const stop = listenOnce({
      onText: (t, final) => {
        setInterim(t)
        if (final) void send(t, 'speech')
      },
      onError: (r) => {
        setListening(false)
        setMicNote(r === 'not-allowed' ? 'Microphone blocked. Tap an answer or type instead.' : `Voice input stopped (${r}). Tap an answer or type instead.`)
      },
      onEnd: () => setListening(false),
    })
    if (!stop) {
      setMicNote('Voice input is not available. Tap an answer or type instead.')
      return
    }
    stopRef.current = stop
    setListening(true)
  }

  const end = async () => {
    stopRef.current?.()
    stopSpeaking()
    try {
      await api.post(`/api/calls/${session.call_id}/end`)
    } catch {
      /* the call is simulated: ending it locally is enough */
    }
    setEnded(true)
    onEnded()
  }

  const endRef = useRef(end)
  endRef.current = end
  // any other activity (a reply, typing, the mic) restarts the 10 s; "I need help after all" clears autoEnd
  useEffect(() => {
    if (!autoEnd || ended || busy || listening || typed || interim) return
    const t = setTimeout(() => void endRef.current(), SAFE_AUTO_END_MS)
    return () => clearTimeout(t)
  }, [autoEnd, ended, busy, listening, typed, interim, lines.length])

  return (
    <section aria-label="Simulated call" className="flex min-h-0 flex-col border-2 border-ink bg-paper">
      <header className="flex flex-wrap items-center gap-3 border-b-2 border-ink bg-ink px-3 py-2 text-bone">
        <span className="font-display text-lg font-bold uppercase tracking-wider">{ended ? 'Call ended' : 'Call in progress'}</span>
        <Stamp tone="warn">Simulated</Stamp>
        <span aria-label="Call timer" className="ml-auto font-mono text-3xl font-semibold leading-none">{mmss(secs)}</span>
      </header>

      <div ref={logRef} aria-live="polite" className="max-h-[38vh] min-h-[200px] flex-1 overflow-y-auto px-3 py-3">
        {lines.map((l, i) => {
          const last = i === lines.length - 1 || (i === lines.length - 2 && lines[lines.length - 1].speaker === 'caller')
          const op = l.speaker === 'operator'
          return (
            <div key={i} className={`mb-3 flex gap-3 ${last ? '' : 'opacity-60'}`}>
              <span className={`w-1.5 shrink-0 ${op ? 'bg-orange' : 'bg-ink'}`} aria-hidden="true" />
              <div>
                <div className="font-mono text-[11px] uppercase text-ink/60">{op ? 'Call-taker (scripted, simulated)' : `You (${l.mode})`}</div>
                <p className={`font-sans font-semibold leading-tight ${last ? 'text-[26px] sm:text-[30px]' : 'text-lg'}`}>{l.text}</p>
              </div>
            </div>
          )
        })}
        {interim && (
          <div className="mb-3 flex gap-3">
            <span className="w-1.5 shrink-0 bg-ink/40" aria-hidden="true" />
            <p className="text-[26px] font-semibold italic leading-tight text-ink/60">{interim}…</p>
          </div>
        )}
        {busy && <p className="font-mono text-xs uppercase text-ink/60">Call-taker is typing…</p>}
      </div>

      {err && (
        <p role="alert" className="border-y-2 border-red bg-red/10 px-3 py-2 text-sm font-semibold">
          {err}
        </p>
      )}

      {!ended && (
        <div className="border-t-2 border-ink px-3 py-3">
          {!finished && q && (
            <div className="mb-3">
              <div className="mb-1.5 font-mono text-[11px] uppercase text-ink/70">Tap an answer, speak, or type</div>
              <div className="grid grid-cols-2 gap-2">
                {QUICK[q].map((a) => (
                  <button
                    key={a}
                    onClick={() => void send(a, 'quick')}
                    disabled={busy}
                    className="min-h-[56px] border-2 border-ink bg-paper px-3 py-2 text-left text-lg font-semibold leading-tight hover:bg-ink hover:text-paper disabled:opacity-50"
                  >
                    {a}
                  </button>
                ))}
              </div>
            </div>
          )}
          {finished && <p className="mb-3 border-l-4 border-green bg-green/10 px-3 py-2 text-lg font-semibold">Stay on the line. You can still add details below.</p>}
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              void send(typed, 'typed')
            }}
          >
            <input
              aria-label="Type your answer"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder="Type here if you cannot speak"
              className="min-h-[56px] min-w-0 flex-1 border-2 border-ink bg-paper px-3 text-lg placeholder:text-ink/50"
            />
            <button type="submit" disabled={busy || !typed.trim()} className="min-h-[56px] min-w-[84px] border-2 border-ink bg-ink px-4 font-display text-xl uppercase text-paper hover:bg-orange hover:text-ink disabled:opacity-40">
              Send
            </button>
          </form>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={mic}
              disabled={!canMic || ended}
              aria-pressed={listening}
              className={`min-h-[56px] min-w-[56px] flex-1 border-2 px-4 font-display text-xl uppercase ${listening ? 'border-red bg-red text-paper' : 'border-ink bg-paper hover:bg-ink hover:text-paper'} disabled:opacity-40`}
            >
              {listening ? 'Listening… tap to stop' : canMic ? 'Speak' : 'Voice not available'}
            </button>
            <button onClick={() => { setMuted((m) => !m); stopSpeaking() }} aria-pressed={muted} className={`min-h-[56px] min-w-[56px] border-2 border-ink px-4 font-display text-xl uppercase ${muted ? 'bg-ink text-paper' : 'bg-paper hover:bg-ink hover:text-paper'}`}>
              {muted ? 'Unmute' : 'Mute'}
            </button>
            <button onClick={end} className="min-h-[56px] min-w-[56px] border-2 border-red bg-red px-4 font-display text-xl uppercase text-paper hover:bg-ink hover:border-ink">
              End call
            </button>
          </div>
          {micNote && <p role="status" className="mt-2 text-sm font-semibold text-red">{micNote}</p>}
          <label className="mt-3 flex min-h-[56px] cursor-pointer items-center gap-3 border-t border-ink/30 pt-3 text-sm">
            <input type="checkbox" checked={scripted} onChange={(e) => setScripted(e.target.checked)} className="h-5 w-5 accent-orange" />
            <span>
              <b>Demo mode:</b> answer for me with a scripted cardiac call (no microphone or key needed)
            </span>
          </label>
        </div>
      )}
    </section>
  )
}
