// Sound, vibration and speech for the SOS countdown and the "Are you OK?" check-in.
// Everything degrades silently: a phone without Web Audio, vibration or speech still gets the large text.

import { api } from './api'

export interface SosTiming {
  holdMs: number
  countdownS: number
}

const DEFAULTS: SosTiming = { holdMs: 2000, countdownS: 10 }
let server: SosTiming | null = null
let override: Partial<SosTiming> | null = null

/** Tests set short timings here so the real flow can run in a fraction of a second. */
export function setTimingOverride(o: Partial<SosTiming> | null) {
  override = o
}
export function getTiming(): SosTiming {
  return { ...DEFAULTS, ...(server ?? {}), ...(override ?? {}) }
}
export async function loadTiming(): Promise<SosTiming> {
  try {
    const c = await api.get<{ hold_ms: number; countdown_s: number }>('/api/false-alarm/config')
    server = { holdMs: c.hold_ms, countdownS: c.countdown_s }
  } catch {
    /* keep the defaults: 2 s hold, 10 s countdown */
  }
  return getTiming()
}

let ctx: AudioContext | null = null
function audio(): AudioContext | null {
  try {
    if (typeof AudioContext === 'undefined') return null
    ctx = ctx ?? new AudioContext()
    if (ctx.state === 'suspended') void ctx.resume()
    return ctx
  } catch {
    return null
  }
}

/** A short square-wave beep through Web Audio. */
export function beep(freq = 880, ms = 140, volume = 0.5) {
  const a = audio()
  if (!a) return
  try {
    const o = a.createOscillator()
    const g = a.createGain()
    o.type = 'square'
    o.frequency.value = freq
    g.gain.value = Math.max(0, Math.min(1, volume)) * 0.35
    o.connect(g)
    g.connect(a.destination)
    o.start()
    o.stop(a.currentTime + ms / 1000)
  } catch {
    /* ignore */
  }
}

export function haptic(pattern: number | number[] = 60) {
  try {
    navigator.vibrate?.(pattern)
  } catch {
    /* ignore */
  }
}

export function speak(text: string, volume = 1) {
  try {
    if (typeof speechSynthesis === 'undefined' || typeof SpeechSynthesisUtterance === 'undefined') return
    speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(text)
    u.volume = Math.max(0, Math.min(1, volume))
    u.rate = 0.95
    speechSynthesis.speak(u)
  } catch {
    /* ignore */
  }
}
