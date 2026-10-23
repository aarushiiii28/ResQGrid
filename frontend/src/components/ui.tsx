import type { ReactNode } from 'react'

export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'mute' | 'ink'

const TONE: Record<Tone, string> = {
  ok: 'border-green text-green',
  warn: 'border-amber text-amber',
  bad: 'border-red text-red',
  info: 'border-orange text-orange',
  mute: 'border-rule/60 text-rule',
  ink: 'border-ink text-ink',
}

const BAR: Record<Tone, string> = {
  ok: 'bg-green',
  warn: 'bg-amber',
  bad: 'bg-red',
  info: 'bg-orange',
  mute: 'bg-rule/50',
  ink: 'bg-ink',
}

/** Square stamp: 1px border, uppercase mono. Always carries text, never colour alone. */
export function Stamp({ tone = 'mute', children, title, className = '' }: { tone?: Tone; children: ReactNode; title?: string; className?: string }) {
  return (
    <span className={`stamp ${TONE[tone]} ${className}`} title={title}>
      {children}
    </span>
  )
}

export function EdgeBar({ tone = 'mute', className = '' }: { tone?: Tone; className?: string }) {
  return <span aria-hidden="true" className={`block w-1 shrink-0 self-stretch ${BAR[tone]} ${className}`} />
}

export function Section({ title, right, children, className = '' }: { title: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`border-b hair ${className}`}>
      <header className="flex items-center justify-between gap-2 px-3 pb-1 pt-3">
        <h3 className="rail-title">{title}</h3>
        {right}
      </header>
      <div className="px-3 pb-3">{children}</div>
    </section>
  )
}

export function Empty({ title, hint, className = '' }: { title: string; hint?: string; className?: string }) {
  return (
    <div role="status" className={`border border-dashed border-rule/40 px-3 py-6 text-center ${className}`}>
      <p className="font-display text-xl uppercase tracking-wide text-bone/80">{title}</p>
      {hint && <p className="mt-1 text-sm text-rule">{hint}</p>}
    </div>
  )
}

export function Loading({ what = 'Loading' }: { what?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-3 px-3 py-6 font-mono text-xs uppercase text-rule">
      <span className="blink inline-block h-2.5 w-2.5 bg-orange" aria-hidden="true" />
      {what}…
    </div>
  )
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start justify-between gap-3 border-l-4 border-red bg-red/10 px-3 py-2 text-sm">
      <div>
        <span className="stamp mr-2 border-red text-red">error</span>
        {message}
      </div>
      {onRetry && (
        <button className="btn-ghost !min-h-[32px] !px-2 !text-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function Mono({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono text-[0.92em] ${className}`}>{children}</span>
}

export function Kv({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b hair py-1 text-sm last:border-0">
      <dt className="shrink-0 font-mono text-[11px] uppercase text-rule">{k}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  )
}

export function Stat({ value, label, tone = 'ink' }: { value: ReactNode; label: string; tone?: 'ink' | 'orange' | 'green' | 'red' | 'amber' }) {
  const c = { ink: 'text-bone', orange: 'text-orange', green: 'text-green', red: 'text-red', amber: 'text-amber' }[tone]
  return (
    <div className="border-l-2 border-rule/40 pl-3">
      <div className={`font-display text-5xl leading-none ${c}`}>{value}</div>
      <div className="mt-1 font-mono text-[11px] uppercase text-rule">{label}</div>
    </div>
  )
}
