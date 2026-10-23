// Hand-built SVG artwork in the field-operations palette. Flat shapes only: no gradients, no shadows.
import type { ReactNode } from 'react'

const INK = '#15120E'
const BONE = '#ECE6D8'
const PAPER = '#F6F2E8'
const ORANGE = '#E4570F'
const RED = '#C0182B'
const AMBER = '#E9A91A'
const GREEN = '#3F7A3A'
const RULE = '#BDB5A3'

export function HazardBand({ height = 14, className = '', dark = false }: { height?: number; className?: string; dark?: boolean }) {
  const id = dark ? 'hz-d' : 'hz-l'
  return (
    <svg aria-hidden="true" className={`block w-full ${className}`} height={height} preserveAspectRatio="none">
      <defs>
        <pattern id={id} width={height * 1.6} height={height} patternUnits="userSpaceOnUse" patternTransform="skewX(-35)">
          <rect width={height * 0.8} height={height} fill={ORANGE} />
          <rect x={height * 0.8} width={height * 0.8} height={height} fill={dark ? INK : INK} />
        </pattern>
      </defs>
      <rect width="100%" height={height} fill={`url(#${id})`} />
    </svg>
  )
}

export function Ambulance({ className = '', moving = false }: { className?: string; moving?: boolean }) {
  return (
    <svg viewBox="0 0 240 110" className={className} role="img" aria-label="Ambulance">
      <title>Ambulance</title>
      {/* speed lines */}
      {moving && (
        <g stroke={RULE} strokeWidth="3" strokeLinecap="square">
          <path d="M4 40h26M0 58h34M8 76h22" className="speed-lines" />
        </g>
      )}
      {/* body */}
      <path d="M40 24h96v52H40z" fill={BONE} stroke={INK} strokeWidth="3" />
      <path d="M136 36h42l22 22v18h-64z" fill={BONE} stroke={INK} strokeWidth="3" />
      <path d="M146 42h28l14 15h-42z" fill={INK} />
      {/* stripe */}
      <path d="M40 58h96v10H40z" fill={ORANGE} />
      <path d="M136 62h64v6h-64z" fill={ORANGE} />
      {/* cross */}
      <path d="M80 30h16v8h8v16h-8v8H80v-8h-8V38h8z" fill={RED} transform="translate(-6 -2) scale(1)" />
      {/* roof light */}
      <rect x="82" y="14" width="30" height="10" fill={RED} stroke={INK} strokeWidth="3" />
      <rect x="86" y="8" width="22" height="6" fill={AMBER} stroke={INK} strokeWidth="2" className={moving ? 'siren-flash' : ''} />
      {/* wheels */}
      <g>
        <circle cx="76" cy="80" r="14" fill={INK} />
        <circle cx="76" cy="80" r="5" fill={RULE} />
        <circle cx="168" cy="80" r="14" fill={INK} />
        <circle cx="168" cy="80" r="5" fill={RULE} />
      </g>
      <path d="M20 94h210" stroke={INK} strokeWidth="2" />
    </svg>
  )
}

/** Octagonal SOS control artwork (stop-sign geometry, not a pill). */
export function Octagon({ size = 280, fill = ORANGE, stroke = INK, children, className = '' }: { size?: number; fill?: string; stroke?: string; children?: ReactNode; className?: string }) {
  const p = (s: number) => {
    const a = s * 0.2929
    return `${a},0 ${s - a},0 ${s},${a} ${s},${s - a} ${s - a},${s} ${a},${s} 0,${s - a} 0,${a}`
  }
  return (
    <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} className={className} aria-hidden="true">
      <polygon points={p(size)} fill={fill} stroke={stroke} strokeWidth="6" strokeLinejoin="miter" />
      <polygon points={p(size - 36)} transform="translate(18 18)" fill="none" stroke={stroke} strokeWidth="2" strokeDasharray="10 8" />
      {children}
    </svg>
  )
}

/** Concentric octagon pulses behind the SOS control. */
export function PulseRings({ size = 340 }: { size?: number }) {
  const poly = (s: number) => {
    const a = s * 0.2929
    return `${a},0 ${s - a},0 ${s},${a} ${s},${s - a} ${s - a},${s} ${a},${s} 0,${s - a} 0,${a}`
  }
  return (
    <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} className="pointer-events-none absolute inset-0 m-auto" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <polygon key={i} points={poly(size)} fill="none" stroke={ORANGE} strokeWidth="3" className="ring-pulse" style={{ animationDelay: `${i * 0.9}s`, transformOrigin: 'center' }} />
      ))}
    </svg>
  )
}

export const Pictogram = {
  caller: ({ className = '' }: { className?: string }) => (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="square">
      <path d="M19 6h26l13 13v26L45 58H19L6 45V19z" />
      <path d="M20 40c4-9 8-14 12-14s8 5 12 14" />
      <path d="M32 26v-8M23 31l-5-5M41 31l5-5" />
    </svg>
  ),
  dispatcher: ({ className = '' }: { className?: string }) => (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="square">
      <path d="M6 10h52v34H6z" />
      <path d="M6 22h52M6 33h52M22 10v34M40 10v34" strokeWidth="2" />
      <path d="M24 54h16M32 44v10" />
      <circle cx="31" cy="28" r="4" fill="currentColor" stroke="none" />
    </svg>
  ),
  driver: ({ className = '' }: { className?: string }) => (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="square">
      <path d="M32 6 52 54 32 44 12 54z" />
      <path d="M32 18v18" strokeWidth="2.5" />
    </svg>
  ),
  hospital: ({ className = '' }: { className?: string }) => (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="square">
      <path d="M8 56V16h48v40z" />
      <path d="M26 56V42h12v14" />
      <path d="M28 20h8v6h6v8h-6v6h-8v-6h-6v-8h6z" fill="currentColor" stroke="none" />
    </svg>
  ),
  admin: ({ className = '' }: { className?: string }) => (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="square">
      <path d="M32 6 54 16v18c0 12-9 20-22 24C19 54 10 46 10 34V16z" />
      <path d="M22 32l8 8 14-16" />
    </svg>
  ),
}

/** Animated city: street grid, a scene beacon, a hospital, and an ambulance tracing the route. */
export function CityMapArt({ className = '' }: { className?: string }) {
  const verts = [60, 140, 230, 330, 420, 520, 610, 700]
  const hors = [60, 150, 240, 330, 420, 510, 600, 690, 780, 860]
  return (
    <svg viewBox="0 0 760 900" className={className} role="img" aria-label="Animated map: an ambulance travelling a route from the scene to a hospital (simulation)" preserveAspectRatio="xMidYMid slice">
      <title>Simulated ambulance route</title>
      <rect width="760" height="900" fill={INK} />
      {/* blocks */}
      <g fill="#201c16">
        <rect x="150" y="70" width="70" height="70" />
        <rect x="340" y="160" width="70" height="70" />
        <rect x="530" y="250" width="70" height="70" />
        <rect x="70" y="340" width="60" height="70" />
        <rect x="430" y="430" width="80" height="70" />
        <rect x="240" y="520" width="80" height="70" />
        <rect x="620" y="610" width="70" height="70" />
        <rect x="70" y="610" width="60" height="70" />
        <rect x="430" y="700" width="80" height="70" />
        <rect x="240" y="790" width="80" height="60" />
      </g>
      {/* river band */}
      <path d="M0 455C140 430 220 490 360 468S600 400 760 440V478C610 446 470 520 340 502S130 470 0 492z" fill="#2a2620" />
      {/* streets */}
      <g stroke="#3b352b" strokeWidth="9" strokeLinecap="square">
        {verts.map((x) => (
          <path key={'v' + x} d={`M${x} 0V900`} />
        ))}
        {hors.map((y) => (
          <path key={'h' + y} d={`M0 ${y}H760`} />
        ))}
      </g>
      <g stroke={RULE} strokeOpacity="0.28" strokeWidth="1" strokeDasharray="10 12">
        {verts.map((x) => (
          <path key={'vd' + x} d={`M${x} 0V900`} />
        ))}
        {hors.map((y) => (
          <path key={'hd' + y} d={`M0 ${y}H760`} />
        ))}
      </g>
      {/* closure marker: the route detours around it */}
      <g transform="translate(330 600)">
        <circle r="24" fill="none" stroke={RED} strokeWidth="3" strokeDasharray="6 5" />
        <path d="M-9-9 9 9M9-9-9 9" stroke={RED} strokeWidth="4" />
      </g>
      {/* route */}
      <path id="route-art" d="M140 780V690H230V600H230V510H520V330H610V150" fill="none" stroke={ORANGE} strokeWidth="6" strokeDasharray="14 10" className="route-march" />
      {/* scene beacon */}
      <g transform="translate(140 780)">
        <circle r="10" fill={ORANGE} />
        <circle r="10" fill="none" stroke={ORANGE} strokeWidth="3" className="beacon" />
        <circle r="10" fill="none" stroke={ORANGE} strokeWidth="3" className="beacon" style={{ animationDelay: '0.8s' }} />
      </g>
      {/* hospital */}
      <g transform="translate(610 150)">
        <rect x="-24" y="-24" width="48" height="48" fill={BONE} stroke={INK} strokeWidth="4" />
        <path d="M-5-14h10v9h9v10h-9v9h-10v-9h-9v-10h9z" fill={RED} />
      </g>
      {/* green corridor signals */}
      {[
        [140, 690],
        [230, 640],
        [230, 540],
        [380, 510],
        [520, 420],
        [570, 330],
      ].map(([x, y], i) => (
        <g key={i} transform={`translate(${x} ${y})`}>
          <rect x="-5" y="-5" width="10" height="10" fill={GREEN} className="signal-go" style={{ animationDelay: `${i * 0.7}s` }} />
        </g>
      ))}
      {/* ambulance */}
      <g>
        <rect x="-15" y="-10" width="30" height="20" fill={BONE} stroke={INK} strokeWidth="2" />
        <rect x="-15" y="2" width="30" height="4" fill={ORANGE} />
        <path d="M-3-7h6v3h3v6h-3v3h-6v-3h-3v-6h3z" fill={RED} />
        <animateMotion dur="11s" repeatCount="indefinite" rotate="0">
          <mpath href="#route-art" />
        </animateMotion>
      </g>
      {/* corner ticks */}
      <g stroke={BONE} strokeOpacity="0.5" strokeWidth="2" fill="none">
        <path d="M14 34V14h20M746 34V14h-20M14 866v20h20M746 866v20h-20" />
      </g>
    </svg>
  )
}
export function FlowGlyph({ kind, className = '' }: { kind: 'sos' | 'pin' | 'call' | 'unit' | 'route' | 'hospital'; className?: string }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 3, strokeLinecap: 'square' as const }
  return (
    <svg viewBox="0 0 48 48" className={className} aria-hidden="true" {...common}>
      {kind === 'sos' && (
        <>
          <path d="M14 4h20l10 10v20L34 44H14L4 34V14z" />
          <path d="M16 20c0-3 3-4 5-4s5 1 5 4-10 4-10 8 3 5 6 5 5-1 5-4" strokeWidth="2.4" />
        </>
      )}
      {kind === 'pin' && (
        <>
          <path d="M24 44C14 30 10 24 10 18a14 14 0 0 1 28 0c0 6-4 12-14 26z" />
          <circle cx="24" cy="18" r="5" />
        </>
      )}
      {kind === 'call' && (
        <>
          <path d="M6 10h36v22H20l-8 8v-8H6z" />
          <path d="M14 18h20M14 25h12" strokeWidth="2.4" />
        </>
      )}
      {kind === 'unit' && (
        <>
          <path d="M4 30V16h24v14M28 20h8l8 8v6H4v-4" />
          <circle cx="14" cy="36" r="4" fill="currentColor" />
          <circle cx="34" cy="36" r="4" fill="currentColor" />
          <path d="M15 20v6M12 23h6" strokeWidth="2.4" />
        </>
      )}
      {kind === 'route' && (
        <>
          <path d="M8 40V24h14V10h18" />
          <circle cx="8" cy="40" r="3" fill="currentColor" />
          <path d="M36 4l6 6-6 6" />
        </>
      )}
      {kind === 'hospital' && (
        <>
          <path d="M6 42V12h36v30z" />
          <path d="M21 17h6v6h6v6h-6v6h-6v-6h-6v-6h6z" fill="currentColor" stroke="none" />
        </>
      )}
    </svg>
  )
}

export const PALETTE = { INK, BONE, PAPER, ORANGE, RED, AMBER, GREEN, RULE }
