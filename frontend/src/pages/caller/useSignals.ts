import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import type { Fix } from '../../lib/geo'
import {
  analyseMotion,
  crashLike,
  feature,
  micFeatures,
  touchFeatures,
  type Feature,
  type MicFrame,
  type MotionSample,
  type TouchEvt,
} from '../../lib/signals'

export type SensorState = 'idle' | 'asking' | 'on' | 'denied' | 'unavailable'

const SEND_EVERY_MS = 5000
const WINDOW_MS = 30000
const DEVICE_KEY = 'resqgrid.device'

/** A random per-install id. The server only ever stores its hash (accidental-press history). */
export function deviceId(): string {
  try {
    let id = localStorage.getItem(DEVICE_KEY)
    if (!id) {
      id = 'dev-' + Math.random().toString(36).slice(2, 12)
      localStorage.setItem(DEVICE_KEY, id)
    }
    return id
  } catch {
    return 'dev-anon'
  }
}

type DME = typeof DeviceMotionEvent & { requestPermission?: () => Promise<'granted' | 'denied'> }

/** Collects phone signals during the SOS flow and sends derived numbers. The microphone is used only after the
 * caller taps "Allow", is analysed in memory (loudness, scream band, impact, silence) and is never recorded. */
export function useSignals(incidentId: string | null, fix: Fix | null) {
  const [motion, setMotion] = useState<SensorState>('idle')
  const [mic, setMic] = useState<SensorState>('idle')
  const samples = useRef<MotionSample[]>([])
  const speeds = useRef<{ t: number; kmh: number }[]>([])
  const touches = useRef<TouchEvt[]>([])
  const holdDone = useRef(false)
  const frames = useRef<MicFrame[]>([])
  const micCleanup = useRef<(() => void) | null>(null)
  const lastSent = useRef('')
  const motionOn = useRef(false)

  // ---- motion ----
  const onMotion = useCallback((e: DeviceMotionEvent) => {
    const a = e.accelerationIncludingGravity
    if (!a || a.x === null || a.y === null || a.z === null) return
    const mag = Math.sqrt(a.x ** 2 + a.y ** 2 + a.z ** 2) / 9.81
    const r = e.rotationRate
    const rot = r ? Math.sqrt((r.alpha ?? 0) ** 2 + (r.beta ?? 0) ** 2 + (r.gamma ?? 0) ** 2) : 0
    const t = Date.now()
    samples.current.push({ t, mag, rot })
    const cut = t - WINDOW_MS
    while (samples.current.length && samples.current[0].t < cut) samples.current.shift()
  }, [])

  /** Call from a user gesture (the SOS hold): iOS only asks for motion access inside one. */
  const requestMotion = useCallback(async () => {
    if (motionOn.current) return
    if (typeof window === 'undefined' || typeof DeviceMotionEvent === 'undefined') {
      setMotion('unavailable')
      return
    }
    setMotion('asking')
    try {
      const req = (DeviceMotionEvent as DME).requestPermission
      if (typeof req === 'function') {
        const r = await req()
        if (r !== 'granted') {
          setMotion('denied')
          return
        }
      }
      window.addEventListener('devicemotion', onMotion)
      motionOn.current = true
      setMotion('on')
    } catch {
      setMotion('denied')
    }
  }, [onMotion])

  useEffect(
    () => () => {
      window.removeEventListener('devicemotion', onMotion)
      micCleanup.current?.()
    },
    [onMotion],
  )

  // ---- GPS speed ----
  useEffect(() => {
    if (!fix || fix.speed_ms === null || fix.speed_ms === undefined) return
    speeds.current.push({ t: Date.now(), kmh: fix.speed_ms * 3.6 })
    const cut = Date.now() - 15000
    speeds.current = speeds.current.filter((s) => s.t >= cut)
  }, [fix])

  // ---- touch ----
  const noteTouch = useCallback((pointers = 1) => {
    touches.current.push({ t: Date.now(), pointers })
    if (touches.current.length > 40) touches.current.shift()
  }, [])
  const noteHold = useCallback((done: boolean) => {
    holdDone.current = done
  }, [])

  // ---- microphone (explicit consent only) ----
  const enableMic = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof AudioContext === 'undefined') {
      setMic('unavailable')
      return
    }
    setMic('asking')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext()
      const src = ctx.createMediaStreamSource(stream)
      const an = ctx.createAnalyser()
      an.fftSize = 1024
      src.connect(an) // not connected to the speakers or to anything that records
      const time = new Float32Array(an.fftSize)
      const freq = new Float32Array(an.frequencyBinCount)
      const hzPerBin = ctx.sampleRate / an.fftSize
      const id = setInterval(() => {
        an.getFloatTimeDomainData(time)
        let sum = 0
        for (const v of time) sum += v * v
        const rms = Math.sqrt(sum / time.length)
        an.getFloatFrequencyData(freq)
        let total = 0
        let band = 0
        for (let i = 1; i < freq.length; i++) {
          const p = 10 ** (freq[i] / 10)
          total += p
          const hz = i * hzPerBin
          if (hz >= 1500 && hz <= 4000) band += p
        }
        frames.current.push({ rms, scream: total > 0 ? band / total : 0 })
        if (frames.current.length > 300) frames.current.shift()
      }, 100)
      micCleanup.current = () => {
        clearInterval(id)
        stream.getTracks().forEach((t) => t.stop())
        void ctx.close()
        frames.current = []
        micCleanup.current = null
      }
      setMic('on')
    } catch {
      setMic('denied')
    }
  }, [])
  const disableMic = useCallback(() => {
    micCleanup.current?.()
    setMic('idle')
  }, [])

  // ---- build and send derived numbers ----
  const build = useCallback((): { features: Feature[]; mic_consent: boolean } => {
    const out: Feature[] = []
    const m = analyseMotion(samples.current)
    if (motion === 'on' && m) {
      const sp = speeds.current
      const maxSpeed = sp.length ? Math.max(...sp.map((s) => s.kmh)) : null
      const nowSpeed = sp.length ? sp[sp.length - 1].kmh : null
      const drop = maxSpeed !== null && nowSpeed !== null ? Math.max(0, maxSpeed - nowSpeed) : null
      out.push(feature('crash_like', crashLike(m.peakG, m.stillS, drop)))
      out.push(feature('motion_erratic', m.erratic), feature('pocket_motion', m.pocket), feature('device_idle', m.idle))
    } else {
      const st = motion === 'denied' ? 'denied' : 'unavailable'
      for (const n of ['crash_like', 'motion_erratic', 'pocket_motion', 'device_idle']) out.push(feature(n, null, st))
    }
    const sp = speeds.current
    out.push(sp.length ? feature('speed_kmh', sp[sp.length - 1].kmh) : feature('speed_kmh', null))
    if (touches.current.length) {
      const t = touchFeatures(touches.current, holdDone.current)
      out.push(feature('touch_deliberate', t.deliberate), feature('touch_random', t.random))
    }
    if (mic === 'on') {
      const mf = micFeatures(frames.current)
      if (mf) {
        out.push(feature('scream', mf.scream), feature('impact_transient', mf.impact), feature('mic_silence', mf.silence), feature('mic_loudness', mf.loudness))
      }
    } else if (mic === 'denied') {
      for (const n of ['scream', 'impact_transient', 'mic_silence']) out.push(feature(n, null, 'denied'))
    }
    return { features: out, mic_consent: mic === 'on' }
  }, [motion, mic])

  const send = useCallback(async () => {
    if (!incidentId) return
    const b = build()
    const key = JSON.stringify(b)
    if (key === lastSent.current) return
    lastSent.current = key
    try {
      await api.post(`/api/incidents/${incidentId}/signals`, b)
    } catch {
      lastSent.current = '' // retry next time
    }
  }, [incidentId, build])

  useEffect(() => {
    if (!incidentId) return
    void send()
    const id = setInterval(() => void send(), SEND_EVERY_MS)
    return () => clearInterval(id)
  }, [incidentId, send])

  return { motion, mic, requestMotion, enableMic, disableMic, noteTouch, noteHold, send }
}
