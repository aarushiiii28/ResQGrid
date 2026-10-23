import { useCallback, useEffect, useRef, useState } from 'react'
import { Octagon } from '../../components/art'
import { haptic } from '../../lib/alerts'

interface Props {
  holdMs: number
  disabled?: boolean
  busyLabel?: string
  onHoldStart?: () => void
  onTouch?: (pointers: number) => void
  onComplete: () => void
}

const R = 150 // ring radius in the 320 box
const C = 2 * Math.PI * R

/** SOS needs a deliberate press-and-hold with a visible progress ring. Releasing early cancels. Space or
 * Enter held down works the same way for keyboard and switch users. */
export default function HoldSos({ holdMs, disabled, busyLabel, onHoldStart, onTouch, onComplete }: Props) {
  const [progress, setProgress] = useState(0) // 0..1
  const raf = useRef<number | null>(null)
  const startedAt = useRef<number | null>(null)
  const pointers = useRef(new Set<number>())
  const done = useRef(false)

  const stop = useCallback(() => {
    if (raf.current !== null) cancelAnimationFrame(raf.current)
    raf.current = null
    startedAt.current = null
    setProgress(0)
  }, [])

  const tick = useCallback(() => {
    if (startedAt.current === null) return
    const p = Math.min(1, (performance.now() - startedAt.current) / holdMs)
    setProgress(p)
    if (p >= 1) {
      done.current = true
      raf.current = null
      startedAt.current = null
      haptic([80, 40, 80])
      onComplete()
      return
    }
    raf.current = requestAnimationFrame(tick)
  }, [holdMs, onComplete])

  const begin = useCallback(
    (pointerId: number) => {
      if (disabled || done.current) return
      pointers.current.add(pointerId)
      onTouch?.(pointers.current.size)
      if (startedAt.current !== null) return
      onHoldStart?.()
      startedAt.current = performance.now()
      haptic(30)
      raf.current = requestAnimationFrame(tick)
    },
    [disabled, onHoldStart, onTouch, tick],
  )
  const end = useCallback(
    (pointerId: number) => {
      pointers.current.delete(pointerId)
      if (pointers.current.size === 0 && !done.current) stop()
    },
    [stop],
  )
  useEffect(() => () => stop(), [stop])
  useEffect(() => {
    if (!disabled) done.current = false
  }, [disabled])

  const pct = Math.round(progress * 100)
  const secs = (holdMs / 1000).toFixed(holdMs % 1000 === 0 ? 0 : 1)
  return (
    <div className="relative grid h-full w-full place-items-center">
      <button
        type="button"
        onPointerDown={(e) => begin(e.pointerId)}
        onPointerUp={(e) => end(e.pointerId)}
        onPointerLeave={(e) => end(e.pointerId)}
        onPointerCancel={(e) => end(e.pointerId)}
        onKeyDown={(e) => {
          if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) begin(-1)
        }}
        onKeyUp={(e) => {
          if (e.key === ' ' || e.key === 'Enter') end(-1)
        }}
        onContextMenu={(e) => e.preventDefault()}
        disabled={disabled}
        aria-label={`SOS. Press and hold for ${secs} seconds to request help`}
        aria-describedby="sos-hold-hint"
        className="group relative z-10 touch-none select-none disabled:opacity-90"
        style={{ width: 'min(72vw, 320px)', height: 'min(72vw, 320px)' }}
      >
        <Octagon size={320} className="h-full w-full">
          <text x="160" y="190" textAnchor="middle" fontFamily="Barlow Condensed" fontWeight="700" fontSize="150" fill="#15120E" letterSpacing="2">
            SOS
          </text>
          <text x="160" y="236" textAnchor="middle" fontFamily="IBM Plex Mono" fontWeight="600" fontSize="19" fill="#15120E" letterSpacing="3">
            {busyLabel ?? (progress > 0 ? `HOLD ${pct}%` : `HOLD ${secs} S`)}
          </text>
        </Octagon>
        <svg viewBox="0 0 320 320" className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
          <circle cx="160" cy="160" r={R} fill="none" stroke="#15120E" strokeOpacity="0.18" strokeWidth="10" />
          <circle
            data-testid="hold-ring"
            cx="160"
            cy="160"
            r={R}
            fill="none"
            stroke="#15120E"
            strokeWidth="10"
            strokeDasharray={C}
            strokeDashoffset={C * (1 - progress)}
            transform="rotate(-90 160 160)"
          />
        </svg>
      </button>
      <span id="sos-hold-hint" className="sr-only">
        Keep holding until the ring is full. Let go to cancel.
      </span>
      <div role="progressbar" aria-label="SOS hold progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} className="sr-only" />
    </div>
  )
}
