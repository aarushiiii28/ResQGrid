// Phone signals for the false-alarm model (M18, layer 2). Everything is derived ON THE DEVICE and sent as
// numbers with a status. Nothing here records, stores or uploads audio or raw sensor streams.
// A feature that cannot be read is "unavailable" or "denied": the server treats that as unknown and it never
// lowers the score.

export type FeatureStatus = 'available' | 'unavailable' | 'denied'
export interface Feature {
  name: string
  value: number | null
  status: FeatureStatus
  confidence?: number | null
  at?: string
}

const clamp = (x: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, x))
const r3 = (x: number) => Math.round(x * 1000) / 1000

/** Same arithmetic as the server's crash_like_confidence: spike, then stillness, plus a sudden speed drop. */
export function crashLike(peakG: number | null, stillS: number | null, speedDropKmh: number | null): number | null {
  if (peakG === null) return null
  const spike = clamp((peakG - 2) / 4)
  if (spike <= 0) return 0
  const still = clamp((stillS ?? 0) / 8)
  const drop = clamp((speedDropKmh ?? 0) / 40)
  return r3(clamp(spike * (0.55 + 0.45 * still) + 0.25 * drop * spike))
}

export interface MotionSample {
  t: number // ms
  mag: number // total acceleration in g (1 at rest)
  rot: number // rotation rate, deg/s
}

/** Crash/fall: the largest spike in the window, and how long the phone stayed still after it. */
export function analyseMotion(samples: MotionSample[]) {
  if (samples.length < 5) return null
  let peak = 0
  let peakAt = 0
  for (const s of samples) {
    if (s.mag > peak) {
      peak = s.mag
      peakAt = s.t
    }
  }
  const after = samples.filter((s) => s.t > peakAt)
  let stillS = 0
  if (after.length) {
    let lastMoving = peakAt
    for (const s of after) if (Math.abs(s.mag - 1) > 0.08) lastMoving = s.t
    stillS = (after[after.length - 1].t - lastMoving) / 1000
  }
  const devs = samples.map((s) => Math.abs(s.mag - 1))
  const mean = devs.reduce((a, b) => a + b, 0) / devs.length
  const sd = Math.sqrt(devs.reduce((a, b) => a + (b - mean) ** 2, 0) / devs.length)
  const idle = devs.filter((d) => d < 0.03).length / devs.length
  // pocket / walking: steady, moderate, rhythmic movement at 1 to 3 steps a second
  let crossings = 0
  for (let i = 1; i < samples.length; i++) if ((samples[i].mag - 1) * (samples[i - 1].mag - 1) < 0) crossings++
  const secs = Math.max(1, (samples[samples.length - 1].t - samples[0].t) / 1000)
  const hz = crossings / 2 / secs
  const rhythmic = hz >= 1 && hz <= 3 ? 1 : 0
  const pocket = rhythmic * clamp(mean / 0.15) * (mean < 0.7 ? 1 : 0.3)
  return {
    peakG: peak,
    stillS,
    erratic: clamp(sd / 0.8),
    idle,
    pocket: clamp(pocket),
  }
}

export interface TouchEvt {
  t: number
  pointers: number
}

/** Touch pattern during the SOS flow: a deliberate press is a few steady taps; random touches are many,
 * uneven and multi-finger (a child or a pocket). */
export function touchFeatures(evts: TouchEvt[], holdCompleted: boolean) {
  const taps = evts.length
  const gaps = evts.slice(1).map((e, i) => e.t - evts[i].t)
  const mean = gaps.length ? gaps.reduce((a, b) => a + b, 0) / gaps.length : 0
  const sd = gaps.length ? Math.sqrt(gaps.reduce((a, b) => a + (b - mean) ** 2, 0) / gaps.length) : 0
  const cv = mean > 0 ? sd / mean : 0
  const multi = taps ? evts.filter((e) => e.pointers > 1).length / taps : 0
  const random = clamp(0.12 * Math.max(0, taps - 3) + 0.4 * clamp(cv / 1.5) + 0.5 * multi)
  const deliberate = holdCompleted ? clamp(1 - random) : 0
  return { taps, random: r3(random), deliberate: r3(deliberate) }
}

/** On-device microphone features from one analyser frame series. Only used after explicit consent. */
export interface MicFrame {
  rms: number // 0..1
  scream: number // share of energy in the 1.5 to 4 kHz band, 0..1
}
export function micFeatures(frames: MicFrame[]) {
  if (frames.length < 5) return null
  const loud = frames.map((f) => f.rms)
  const mean = loud.reduce((a, b) => a + b, 0) / loud.length
  let jump = 0
  for (let i = 1; i < loud.length; i++) jump = Math.max(jump, loud[i] - loud[i - 1])
  const silence = frames.filter((f) => f.rms < 0.02).length / frames.length
  const loudFrames = frames.filter((f) => f.rms > 0.15)
  const scream = loudFrames.length ? loudFrames.reduce((a, f) => a + f.scream, 0) / loudFrames.length : 0
  return {
    loudness: r3(clamp(mean * 4)),
    scream: r3(clamp(scream * clamp(loudFrames.length / 8) * 1.3)),
    impact: r3(clamp(jump * 3)),
    silence: r3(silence),
  }
}

export const feature = (name: string, value: number | null, status: FeatureStatus = 'available'): Feature => ({
  name,
  value: value === null ? null : r3(value),
  status: value === null && status === 'available' ? 'unavailable' : status,
})
