import { describe, expect, it } from 'vitest'
import { analyseMotion, crashLike, feature, micFeatures, touchFeatures, type MotionSample } from './signals'

const rest = (n: number, start = 0): MotionSample[] => Array.from({ length: n }, (_, i) => ({ t: start + i * 100, mag: 1, rot: 0 }))

describe('device-side signal features', () => {
  it('crash_like: no accelerometer is unknown, no spike is zero, spike + stillness + speed drop is high (matches the server)', () => {
    expect(crashLike(null, 5, 30)).toBeNull()
    expect(crashLike(1.2, 9, 50)).toBe(0)
    expect(crashLike(8, 9, 45)).toBe(1)
    expect(crashLike(3, 1, 0)).toBe(0.152)
  })

  it('finds an acceleration spike followed by stillness', () => {
    const s = [...rest(20), { t: 2000, mag: 6.5, rot: 300 }, { t: 2100, mag: 2, rot: 80 }, ...rest(60, 2200)]
    const m = analyseMotion(s)!
    expect(m.peakG).toBeCloseTo(6.5)
    expect(m.stillS).toBeGreaterThan(4)
    expect(crashLike(m.peakG, m.stillS, 30)!).toBeGreaterThan(0.8)
  })

  it('a phone lying still is idle, not a crash', () => {
    const m = analyseMotion(rest(100))!
    expect(m.idle).toBeGreaterThan(0.95)
    expect(m.peakG).toBeLessThan(1.1)
    expect(crashLike(m.peakG, m.stillS, 0)).toBe(0)
  })

  it('walking in a pocket looks rhythmic and moderate', () => {
    const s: MotionSample[] = Array.from({ length: 200 }, (_, i) => ({ t: i * 50, mag: 1 + 0.3 * Math.sin((i * 50 * 2 * Math.PI * 1.8) / 1000), rot: 20 }))
    expect(analyseMotion(s)!.pocket).toBeGreaterThan(0.5)
  })

  it('too few samples gives no reading (unavailable), not zero', () => {
    expect(analyseMotion(rest(3))).toBeNull()
  })

  it('touch pattern: a steady hold is deliberate, a burst of uneven multi-finger taps is random', () => {
    const calm = touchFeatures([{ t: 0, pointers: 1 }], true)
    expect(calm.deliberate).toBeGreaterThan(0.9)
    expect(calm.random).toBeLessThan(0.2)
    const burst = Array.from({ length: 12 }, (_, i) => ({ t: i * (i % 2 ? 90 : 400), pointers: i % 3 === 0 ? 2 : 1 }))
    const kid = touchFeatures(burst, false)
    expect(kid.random).toBeGreaterThan(0.7)
    expect(kid.deliberate).toBe(0)
  })

  it('microphone features: scream needs loud frames with energy in the scream band; silence is counted', () => {
    const quiet = Array.from({ length: 30 }, () => ({ rms: 0.005, scream: 0.1 }))
    expect(micFeatures(quiet)!.silence).toBe(1)
    expect(micFeatures(quiet)!.scream).toBe(0)
    const loud = Array.from({ length: 30 }, (_, i) => ({ rms: i === 10 ? 0.7 : 0.4, scream: 0.7 }))
    const m = micFeatures(loud)!
    expect(m.scream).toBeGreaterThan(0.6)
    expect(m.impact).toBeGreaterThan(0.5)
    expect(micFeatures(loud.slice(0, 2))).toBeNull()
  })

  it('a feature that cannot be read is sent as unavailable or denied, never as zero', () => {
    expect(feature('crash_like', null)).toEqual({ name: 'crash_like', value: null, status: 'unavailable' })
    expect(feature('scream', null, 'denied').status).toBe('denied')
    expect(feature('touch_random', 0.1234567)).toMatchObject({ value: 0.123, status: 'available' })
  })
})
