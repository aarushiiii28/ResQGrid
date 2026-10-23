// Charts in ink / orange / green on bone. No default chart colours, no blue or purple.
export const POLICY_COLOR: Record<string, string> = { P0: '#15120E', P1: '#E4570F', P2: '#3F7A3A' }
export const POLICY_NAME: Record<string, string> = { P0: 'P0 baseline', P1: 'P1 optimized', P2: 'P2 ResQGrid' }

interface Bar {
  label: string
  values: Record<string, number | null | undefined>
}

/** Grouped horizontal bars: one group per metric, one bar per policy. Values print at the end of each bar. */
export function BarChart({ bars, policies, unit = '', width = 560, title }: { bars: Bar[]; policies: string[]; unit?: string; width?: number; title: string }) {
  const rowH = 20
  const gap = 18
  const left = 150
  const groupH = policies.length * rowH + gap
  const max = Math.max(1, ...bars.flatMap((b) => policies.map((p) => b.values[p] ?? 0)))
  const h = bars.length * groupH + 8
  return (
    <figure className="bg-bone text-ink">
      <figcaption className="border-b-2 border-ink px-3 py-1 font-display text-xl font-bold uppercase">{title}</figcaption>
      <svg viewBox={`0 0 ${width} ${h}`} className="block w-full" role="img" aria-label={title}>
        <title>{title}</title>
        {bars.map((b, gi) => (
          <g key={b.label} transform={`translate(0 ${gi * groupH + 6})`}>
            <text x="8" y={(policies.length * rowH) / 2 + 4} fontFamily="Barlow Condensed" fontWeight="700" fontSize="17" fill="#15120E" style={{ textTransform: 'uppercase' }}>
              {b.label}
            </text>
            {policies.map((p, i) => {
              const v = b.values[p]
              const w = v == null ? 0 : ((width - left - 70) * v) / max
              return (
                <g key={p} transform={`translate(${left} ${i * rowH})`}>
                  <rect x="0" y="2" width={Math.max(w, v == null ? 0 : 2)} height={rowH - 5} fill={POLICY_COLOR[p]} />
                  <text x={Math.max(w, 2) + 6} y={rowH - 6} fontFamily="IBM Plex Mono" fontSize="12" fill="#15120E">
                    {v == null ? 'n/a' : `${Math.round(v * 10) / 10}${unit}`} <tspan fill="#8a8272">{p}</tspan>
                  </text>
                </g>
              )
            })}
          </g>
        ))}
        <line x1={left} x2={left} y1="0" y2={h} stroke="#15120E" strokeWidth="2" />
      </svg>
    </figure>
  )
}

export function Legend({ policies }: { policies: string[] }) {
  return (
    <div className="flex flex-wrap gap-3 font-mono text-[11px] uppercase">
      {policies.map((p) => (
        <span key={p} className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-3" style={{ background: POLICY_COLOR[p], border: p === 'P0' ? '1px solid #BDB5A3' : undefined }} />
          {POLICY_NAME[p]}
        </span>
      ))}
    </div>
  )
}

/** One row per policy: every seed's value as a dot, with the median as a tick. Shows the whole distribution. */
export function DotStrip({ title, values, medianOf, unit = '' }: { title: string; values: Record<string, number[]>; medianOf: Record<string, number | undefined>; unit?: string }) {
  const pols = Object.keys(values)
  const all = pols.flatMap((p) => values[p])
  if (all.length === 0) return null
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const span = hi - lo || 1
  const W = 560
  const left = 110
  const x = (v: number) => left + ((W - left - 30) * (v - lo)) / span
  return (
    <figure className="bg-bone text-ink">
      <figcaption className="border-b-2 border-ink px-3 py-1 font-display text-xl font-bold uppercase">{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${pols.length * 34 + 30}`} className="block w-full" role="img" aria-label={title}>
        <title>{title}</title>
        {pols.map((p, i) => (
          <g key={p} transform={`translate(0 ${i * 34 + 8})`}>
            <text x="8" y="19" fontFamily="Barlow Condensed" fontWeight="700" fontSize="16" fill="#15120E">{POLICY_NAME[p].toUpperCase()}</text>
            <line x1={left} x2={W - 30} y1="14" y2="14" stroke="#BDB5A3" strokeWidth="1" />
            {values[p].map((v, k) => (
              <rect key={k} x={x(v) - 3.5} y="10.5" width="7" height="7" fill={POLICY_COLOR[p]} fillOpacity="0.75" />
            ))}
            {medianOf[p] !== undefined && <line x1={x(medianOf[p]!)} x2={x(medianOf[p]!)} y1="2" y2="26" stroke={POLICY_COLOR[p]} strokeWidth="3" />}
          </g>
        ))}
        <text x={left} y={pols.length * 34 + 22} fontFamily="IBM Plex Mono" fontSize="11" fill="#15120E">{Math.round(lo)}{unit}</text>
        <text x={W - 30} y={pols.length * 34 + 22} textAnchor="end" fontFamily="IBM Plex Mono" fontSize="11" fill="#15120E">{Math.round(hi)}{unit}</text>
      </svg>
      <p className="px-3 pb-2 font-mono text-[10px] uppercase text-ink/70">Each square is one seed. The bar is the median.</p>
    </figure>
  )
}
