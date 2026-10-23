import { useCallback, useEffect, useState } from 'react'
import { FlowGlyph, HazardBand, PulseRings } from '../../components/art'
import { ErrorBox, Stamp } from '../../components/ui'
import { api } from '../../lib/api'
import type { Aed, Tracker } from '../../lib/types'
import CallPanel from './CallPanel'
import CheckIn from './CheckIn'
import Countdown from './Countdown'
import { AedPanel, FirstAidPanel } from './HelpPanels'
import HoldSos from './HoldSos'
import SignalConsent from './SignalConsent'
import TrackerPanel from './TrackerPanel'
import { useSignals } from './useSignals'
import { useSos } from './useSos'

type Tab = 'status' | 'aid' | 'aed'

const STEPS = [
  { g: 'pin', t: 'Your location is shared', d: 'With its accuracy and age.' },
  { g: 'call', t: 'A simulated call opens', d: 'Speak, tap or type. Three questions at most.' },
  { g: 'unit', t: 'The nearest capable ambulance is chosen', d: 'Approved automatically in this demo.' },
  { g: 'route', t: 'You watch it come', d: 'Live map, plain-text updates.' },
] as const

export default function CallerPage() {
  const sos = useSos()
  const signals = useSignals(sos.pendingId ?? sos.session?.incident_id ?? null, sos.fix)
  const [tab, setTab] = useState<Tab>('status')
  const [tracker, setTracker] = useState<Tracker | null>(null)
  const [aeds, setAeds] = useState<Aed[]>([])
  const [ended, setEnded] = useState(false)
  const inc = sos.session?.incident_id

  const loadAeds = useCallback(() => {
    if (!inc) return
    api.get<Aed[]>(`/api/incidents/${inc}/aed`).then(setAeds).catch(() => {})
  }, [inc])
  useEffect(() => {
    loadAeds()
  }, [loadAeds, sos.pin?.lat, sos.pin?.lon])

  if (sos.phase === 'countdown' || sos.phase === 'confirming' || sos.phase === 'cancelling') {
    return (
      <Countdown
        seconds={sos.timing.countdownS}
        cancelling={sos.phase === 'cancelling'}
        onDone={() => void sos.confirm()}
        onCancel={() => void sos.cancel()}
        onTouch={signals.noteTouch}
      />
    )
  }

  if (sos.phase === 'cancelled' && sos.cancelledId) {
    return (
      <div className="paper-grid relative flex min-h-full flex-col">
        <HazardBand height={16} />
        <div className="mx-auto w-full max-w-2xl flex-1 space-y-4 px-4 py-8">
          <Stamp tone="ink">Request cancelled</Stamp>
          <h1 className="font-display text-5xl font-bold uppercase leading-none">SOS cancelled</h1>
          <p className="text-xl font-semibold leading-snug">
            No ambulance was sent. The record is kept, and we will check once that you are safe. If you need help, tap below or dial 112.
          </p>
          <CheckIn incidentId={sos.cancelledId} />
          <button onClick={sos.reset} className="min-h-[56px] w-full border-2 border-ink px-4 font-display text-xl uppercase hover:bg-ink hover:text-paper">
            Start a new request
          </button>
        </div>
        <EmergencyBar />
      </div>
    )
  }

  if (!sos.session) {
    return (
      <div className="paper-grid relative flex min-h-full flex-col">
        <HazardBand height={16} />
        <div className="mx-auto grid w-full max-w-6xl flex-1 gap-8 px-4 py-6 lg:grid-cols-[1fr_0.9fr] lg:items-center lg:gap-14 lg:py-10">
          <div className="flex flex-col items-center lg:items-start">
            <div className="mb-3 flex flex-wrap items-center gap-2 self-start">
              <Stamp tone="ink">Emergency request</Stamp>
              <Stamp tone="ink">Simulated call</Stamp>
            </div>
            <div className="relative grid place-items-center" style={{ width: 'min(86vw, 400px)', height: 'min(86vw, 400px)' }}>
              <PulseRings size={400} />
              <HoldSos
                holdMs={sos.timing.holdMs}
                disabled={sos.pressed}
                busyLabel={sos.pressed ? 'CONNECTING' : undefined}
                onHoldStart={() => void signals.requestMotion()}
                onTouch={signals.noteTouch}
                onComplete={() => {
                  signals.noteHold(true)
                  sos.begin()
                }}
              />
            </div>
            <p className="mt-2 max-w-md text-xl font-semibold leading-snug">
              Press and hold SOS for {(sos.timing.holdMs / 1000).toFixed(sos.timing.holdMs % 1000 === 0 ? 0 : 1)} seconds, then stay where you are. You can cancel during the countdown. You do not need to fill in anything.
            </p>
            {sos.pressed && (
              <p role="status" className="mt-3 border-l-4 border-orange bg-orange/15 px-3 py-2 text-lg font-semibold">
                {sos.gps.state === 'asking' ? 'Getting your location…' : sos.gps.state === 'denied' ? 'Location is blocked. Using an approximate pin.' : 'Starting the call…'}
              </p>
            )}
            {sos.error && <div className="mt-3 w-full"><ErrorBox message={sos.error} onRetry={sos.begin} /></div>}
          </div>

          <div>
            <h2 className="rail-title !text-base !text-ink">What happens when you press</h2>
            <ol className="mt-3 border-t-2 border-ink">
              {STEPS.map((s, i) => (
                <li key={s.t} className="flex items-center gap-4 border-b-2 border-ink py-3.5">
                  <span className="font-display text-4xl font-bold leading-none text-orange">{i + 1}</span>
                  <FlowGlyph kind={s.g} className="h-10 w-10 shrink-0" />
                  <div>
                    <div className="text-xl font-semibold leading-tight">{s.t}</div>
                    <div className="text-sm text-ink/70">{s.d}</div>
                  </div>
                </li>
              ))}
            </ol>
            <p className="mt-4 text-sm text-ink/70">
              This is a demonstration. No real call, text message or emergency service is involved. Locations and people are synthetic.
            </p>
          </div>
        </div>
        <EmergencyBar />
      </div>
    )
  }

  const category = tracker?.category ?? 'unknown'
  const closed = tracker?.status === 'closed'
  return (
    <div className="paper-grid min-h-full">
      <HazardBand height={10} />
      <div className="mx-auto max-w-7xl px-3 pb-24 pt-3">
        <div className="grid gap-3 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div className="space-y-3">
            <CheckIn incidentId={sos.session.incident_id} />
            <CallPanel session={sos.session} onEnded={() => setEnded(true)} />
            <SignalConsent mic={signals.mic} onAllow={() => void signals.enableMic()} onStop={signals.disableMic} />
            {sos.error && <ErrorBox message={sos.error} />}
            {(ended || closed) && (
              <button onClick={sos.reset} className="min-h-[56px] w-full border-2 border-ink px-4 font-display text-xl uppercase hover:bg-ink hover:text-paper">
                {closed ? 'Close this request' : 'Start a new request'}
              </button>
            )}
          </div>
          <div className="space-y-3">
            <div role="tablist" aria-label="Help panels" className="grid grid-cols-3 border-2 border-ink bg-paper">
              {(
                [
                  ['status', 'Status'],
                  ['aid', 'First aid'],
                  ['aed', 'AED + help'],
                ] as [Tab, string][]
              ).map(([k, l]) => (
                <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={`min-h-[56px] border-r-2 border-ink px-2 font-display text-xl font-bold uppercase last:border-r-0 ${tab === k ? 'bg-ink text-paper' : 'hover:bg-ink/10'}`}>
                  {l}
                </button>
              ))}
            </div>
            <div hidden={tab !== 'status'}>
              <TrackerPanel incidentId={sos.session.incident_id} fix={sos.fix} pin={sos.pin} onMovePin={sos.movePin} aeds={aeds} onTracker={setTracker} />
            </div>
            {tab === 'aid' && <FirstAidPanel category={category} />}
            {tab === 'aed' && <AedPanel incidentId={sos.session.incident_id} aeds={aeds} category={category} />}
          </div>
        </div>
      </div>
      <EmergencyBar />
    </div>
  )
}

function EmergencyBar() {
  return (
    <footer className="fixed inset-x-0 bottom-0 z-[1100] border-t-4 border-ink bg-amber px-3 py-2 text-center text-ink">
      <span className="font-display text-xl font-bold uppercase tracking-wide">In a real emergency dial 112.</span>
      <span className="ml-3 hidden font-mono text-xs uppercase sm:inline">This app is a simulation</span>
    </footer>
  )
}
