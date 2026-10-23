import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'
import { setTimingOverride } from '../lib/alerts'

// the real hold is 2 s and the real countdown 10 s; tests run the same flow in a fraction of a second
beforeEach(() => setTimingOverride({ holdMs: 60, countdownS: 0.4 }))

afterEach(() => {
  setTimingOverride(null)
  cleanup()
  vi.restoreAllMocks()
  try {
    sessionStorage.clear()
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

// jsdom has no ResizeObserver / matchMedia / scrollTo
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO
Element.prototype.scrollTo = (() => {}) as typeof Element.prototype.scrollTo
