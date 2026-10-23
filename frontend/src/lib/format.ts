export function ageText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'never'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

export function ageSince(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null
  return Math.max(0, (now - new Date(iso).getTime()) / 1000)
}

export function etaRange(lo?: number | null, hi?: number | null): string {
  if (lo == null || hi == null) return 'ETA unknown'
  const a = Math.max(1, Math.round(lo / 60))
  const b = Math.max(1, Math.round(hi / 60))
  return a === b ? `${a} min` : `${a}-${b} min`
}

export function dist(m: number | null | undefined): string {
  if (m == null) return ''
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 10) * 10} m`
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return '--:--:--'
  return new Date(iso).toLocaleTimeString([], { hour12: false })
}

export function mmss(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function label(key: string): string {
  return key.replace(/_/g, ' ')
}

export function valueText(v: unknown): string {
  if (v === true) return 'yes'
  if (v === false) return 'no'
  if (v === null || v === undefined) return 'unknown'
  return String(v)
}

export const PRIORITY_COLOR: Record<number, string> = {
  1: 'bg-red',
  2: 'bg-orange',
  3: 'bg-amber',
  4: 'bg-green',
}

export const STATUS_TONE: Record<string, string> = {
  new: 'text-amber',
  dispatched: 'text-orange',
  active: 'text-green',
  closed: 'text-rule',
  merged: 'text-rule',
}
