import { useEffect, useRef, useState } from 'react'
import { beep, haptic } from '../../lib/alerts'

interface Props {
  seconds: number
  cancelling?: boolean
  onDone: () => void
  onCancel: () => void
  onTouch?: (pointers: number) => void
}

/** The cancel window after the hold. Large numerals, a beep and a haptic pulse every second, and one huge
 * Cancel button. When it reaches zero the request goes live. */
export default function Countdown({ seconds, cancelling, onDone, onCancel, onTouch }: Props) {
  const [left, setLeft] = useState(seconds)
  const lastWhole = useRef(Math.ceil(seconds))
  const done = useRef(false)

  useEffect(() => {
    const end = performance.now() + seconds * 1000
    beep(660, 120)
    haptic(40)
    const id = setInterval(() => {
      const rest = Math.max(0, (end - performance.now()) / 1000)
      setLeft(rest)
      const whole = Math.ceil(rest)
      if (whole !== lastWhole.current && whole > 0) {
        lastWhole.current = whole
        beep(whole <= 3 ? 990 : 740, 120)
        haptic(whole <= 3 ? [60, 40, 60] : 40)
      }
      if (rest <= 0 && !done.current) {
        done.current = true
        clearInterval(id)
        beep(1200, 380)
        haptic([120, 60, 120])
        onDone()
      }
    }, 100)
    return () => clearInterval(id)
    // the countdown starts once; callbacks are read through refs below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seconds])

  const shown = Math.max(0, Math.ceil(left))
  return (
    <div role="alertdialog" aria-labelledby="cd-title" aria-describedby="cd-desc" className="paper-grid flex min-h-full flex-col bg-paper">
      <div className="border-b-4 border-ink bg-amber px-4 py-2 text-center">
        <span className="font-display text-2xl font-bold uppercase tracking-wide">Sending SOS</span>
      </div>
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center gap-4 px-4 py-6 text-center">
        <h1 id="cd-title" className="font-display text-3xl font-bold uppercase leading-tight">
          Help is sent in
        </h1>
        <div aria-live="polite" aria-atomic="true" data-testid="countdown-number" className="font-display font-bold leading-none text-ink" style={{ fontSize: 'min(52vw, 280px)' }}>
          {shown}
        </div>
        <p id="cd-desc" className="max-w-md text-xl font-semibold leading-snug">
          Pressed by accident? Cancel now. The request is kept on record, and we will check that you are safe.
        </p>
        <button
          type="button"
          onClick={onCancel}
          onPointerDown={(e) => onTouch?.(e.isPrimary ? 1 : 2)}
          disabled={cancelling}
          className="min-h-[120px] w-full max-w-2xl border-4 border-ink bg-paper px-6 font-display text-6xl font-bold uppercase tracking-wide text-ink hover:bg-ink hover:text-paper disabled:opacity-60"
        >
          {cancelling ? 'Cancelling…' : 'Cancel'}
        </button>
        <p className="font-mono text-xs uppercase text-ink/70">Do nothing to send. Simulated call. In a real emergency dial 112.</p>
      </div>
    </div>
  )
}
