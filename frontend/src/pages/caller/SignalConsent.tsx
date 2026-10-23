import type { SensorState } from './useSignals'

/** Microphone analysis is opt-in. It measures loudness and a few sound shapes in memory on this phone; no audio
 * is recorded, stored or sent. Declining changes nothing: unknown signals never lower the priority. */
export default function SignalConsent({ mic, onAllow, onStop }: { mic: SensorState; onAllow: () => void; onStop: () => void }) {
  return (
    <section aria-label="Microphone analysis" className="border-2 border-ink bg-paper px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-display text-xl font-bold uppercase leading-none">Sound check (optional)</h2>
          <p className="mt-1 max-w-md text-sm">
            Allow microphone analysis to help tell real emergencies from accidents. It runs only on this phone. Nothing is recorded or sent, only a few numbers like loudness.
          </p>
        </div>
        {mic === 'on' ? (
          <button className="min-h-[56px] border-2 border-ink px-4 font-display text-xl uppercase hover:bg-ink hover:text-paper" onClick={onStop} aria-pressed="true">
            Stop sound check
          </button>
        ) : (
          <button
            className="min-h-[56px] border-2 border-ink bg-paper px-4 font-display text-xl uppercase hover:bg-ink hover:text-paper disabled:opacity-50"
            onClick={onAllow}
            disabled={mic === 'asking' || mic === 'unavailable'}
          >
            {mic === 'asking' ? 'Waiting…' : mic === 'unavailable' ? 'Not available' : 'Allow'}
          </button>
        )}
      </div>
      <p role="status" className="mt-1 font-mono text-[11px] uppercase text-ink/70">
        {mic === 'on' ? 'On. Analysing on this phone. No audio is saved.' : mic === 'denied' ? 'Blocked. Counted as unknown, which never lowers your priority.' : mic === 'unavailable' ? 'This phone cannot do it. Counted as unknown.' : 'Off.'}
      </p>
    </section>
  )
}
