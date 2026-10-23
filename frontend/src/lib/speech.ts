// Web Speech API wrappers. Both are optional: every flow also works by tapping or typing.

type SR = {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

export function speechRecognitionSupported(): boolean {
  const w = window as unknown as Record<string, unknown>
  return !!(w.SpeechRecognition || w.webkitSpeechRecognition)
}

export function speechSynthesisSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

export function speak(text: string, opts: { rate?: number; cancel?: boolean } = {}) {
  if (!speechSynthesisSupported()) return
  try {
    if (opts.cancel !== false) window.speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(text)
    u.rate = opts.rate ?? 1
    window.speechSynthesis.speak(u)
  } catch {
    /* speech unavailable */
  }
}

export function stopSpeaking() {
  try {
    window.speechSynthesis?.cancel()
  } catch {
    /* ignore */
  }
}

/** Starts one recognition session. Returns a stop function, or null if unsupported. */
export function listenOnce(handlers: {
  onText: (t: string, final: boolean) => void
  onError: (reason: string) => void
  onEnd: () => void
}): (() => void) | null {
  const w = window as unknown as Record<string, new () => SR>
  const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition
  if (!Ctor) return null
  const r = new Ctor()
  r.lang = 'en-IN'
  r.interimResults = true
  r.continuous = false
  r.onresult = (e) => {
    const res = e.results[e.results.length - 1]
    handlers.onText(res[0].transcript, res.isFinal)
  }
  r.onerror = (e) => handlers.onError(e.error)
  r.onend = handlers.onEnd
  try {
    r.start()
  } catch {
    return null
  }
  return () => {
    try {
      r.stop()
    } catch {
      /* already stopped */
    }
  }
}
