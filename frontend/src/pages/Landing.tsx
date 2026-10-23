import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CityMapArt, FlowGlyph, HazardBand, Pictogram } from '../components/art'
import { Stamp, Stat } from '../components/ui'
import { api } from '../lib/api'
import { HOME, useAuth } from '../lib/auth'
import type { Role } from '../lib/types'

interface Summary {
  units_total: number
  units_available: number
  units_unknown: number
  hospitals_total: number
  hospitals_fresh: number
  open_incidents: number
}

interface DemoUser {
  sub: string
  role: Role
  vehicle_id?: string | null
  hospital_id?: string | null
}

const ROLES: { role: Role; title: string; text: string; icon: keyof typeof Pictogram }[] = [
  { role: 'caller', title: 'Caller', text: 'One SOS button. Location, call and tracking start on their own.', icon: 'caller' },
  { role: 'dispatcher', title: 'Dispatcher', text: 'Supervise the board. Compare ranked units, approve, override.', icon: 'dispatcher' },
  { role: 'driver', title: 'Driver', text: 'Accept the job. Turn-by-turn, reroute prompts, last 100 metres.', icon: 'driver' },
  { role: 'hospital', title: 'Hospital', text: 'Incoming pre-alerts, resource freshness, arrival and handover.', icon: 'hospital' },
  { role: 'admin', title: 'Admin', text: 'Everything a dispatcher sees, plus audit export and demo reset.', icon: 'admin' },
]

const FLOW = [
  { g: 'sos', t: 'Press SOS', d: 'One tap. No forms.' },
  { g: 'pin', t: 'Locate', d: 'GPS, accuracy and age sent with the call.' },
  { g: 'call', t: 'Simulated call', d: 'Speak, type or tap. At most three questions.' },
  { g: 'unit', t: 'Rank and approve', d: 'Nearest capable unit. Human or Demo Autopilot.' },
  { g: 'route', t: 'Navigate', d: 'Turn-by-turn, closures, one-tap reroute.' },
  { g: 'hospital', t: 'Pre-alert', d: 'Hospital sees reported, confirmed and unknown.' },
] as const

export default function Landing() {
  const { user, login } = useAuth()
  const nav = useNavigate()
  const [sum, setSum] = useState<Summary | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [users, setUsers] = useState<DemoUser[]>([])
  const [pick, setPick] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<Role | null>(null)

  useEffect(() => {
    api.get<Summary>('/api/public/summary').then(setSum).catch((e) => setErr(e.message))
    api.get<DemoUser[]>('/api/auth/users').then(setUsers).catch(() => {})
  }, [])

  const enter = async (role: Role) => {
    setBusy(role)
    setErr(null)
    try {
      const u = await login({ role, userId: pick[role] })
      nav(HOME[u.role])
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="min-h-full bg-ink">
      <section className="grid min-h-[calc(100vh-34px)] lg:grid-cols-[1.08fr_1fr]">
        <div className="tile-grid relative flex flex-col border-b hair lg:border-b-0 lg:border-r">
          <HazardBand height={16} dark />
          <div className="flex flex-1 flex-col justify-between gap-8 px-5 py-8 sm:px-10 lg:py-10">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <Stamp tone="info">Field operations board</Stamp>
                <Stamp tone="mute">Simulation</Stamp>
                <Stamp tone="mute">Synthetic data</Stamp>
              </div>
              <h1 className="mt-5 font-display text-[clamp(60px,9.4vw,142px)] font-bold uppercase leading-[0.86] tracking-tight">
                One press.
                <br />
                Everything else
                <br />
                <span className="text-orange">moves.</span>
              </h1>
              <p className="mt-6 max-w-xl text-lg leading-snug text-bone/85">
                A caller presses SOS. Location, call, triage, ambulance choice, route and hospital pre-alert run on their own. Dispatchers supervise. Nothing is assumed available unless it is fresh.
              </p>
              <div className="mt-8 grid max-w-xl grid-cols-3 gap-5">
                <Stat value={sum ? sum.units_available : '–'} label={`of ${sum?.units_total ?? 20} units ready`} tone="green" />
                <Stat value={sum ? sum.hospitals_fresh : '–'} label={`of ${sum?.hospitals_total ?? 12} hospitals fresh`} tone="orange" />
                <Stat value={sum ? sum.open_incidents : '–'} label="open incidents" />
              </div>
              {sum && sum.units_unknown > 0 && (
                <p className="mt-3 font-mono text-[11px] uppercase text-amber">{sum.units_unknown} unit(s) stale: counted as unknown, not available</p>
              )}
              {err && (
                <p role="alert" className="mt-4 border-l-4 border-red bg-red/10 px-3 py-2 text-sm">
                  {err}
                </p>
              )}
            </div>

            <div role="list" aria-label="Choose a role" className="border-t hair">
              {ROLES.map((r) => {
                const Icon = Pictogram[r.icon]
                const sel = r.role === 'driver' ? users.filter((u) => u.role === 'driver') : r.role === 'hospital' ? users.filter((u) => u.role === 'hospital') : []
                return (
                  <div role="listitem" key={r.role} className="group flex items-center gap-4 border-b hair px-1 py-3 transition-colors hover:bg-bone hover:text-ink">
                    <Icon className="h-11 w-11 shrink-0 text-orange group-hover:text-ink" />
                    <div className="min-w-0 flex-1">
                      <div className="font-display text-3xl font-bold uppercase leading-none tracking-wide">{r.title}</div>
                      <div className="mt-0.5 text-sm text-bone/70 group-hover:text-ink/80">{r.text}</div>
                    </div>
                    {sel.length > 0 && (
                      <select
                        aria-label={r.role === 'driver' ? 'Choose unit' : 'Choose hospital'}
                        className="min-h-[40px] border border-rule/60 bg-ink px-2 font-mono text-xs uppercase text-bone"
                        value={pick[r.role] ?? sel[0].sub}
                        onChange={(e) => setPick({ ...pick, [r.role]: e.target.value })}
                      >
                        {sel.map((u) => (
                          <option key={u.sub} value={u.sub}>
                            {u.vehicle_id ?? u.hospital_id}
                          </option>
                        ))}
                      </select>
                    )}
                    <button
                      className="btn-primary !min-h-[44px] shrink-0 group-hover:!border-ink group-hover:!bg-ink group-hover:!text-bone"
                      onClick={() => enter(r.role)}
                      disabled={busy !== null}
                      aria-label={`Enter as ${r.title}`}
                    >
                      {busy === r.role ? 'Entering…' : user?.role === r.role ? 'Resume' : 'Enter'}
                      <span aria-hidden="true">→</span>
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        <div className="relative min-h-[360px] overflow-hidden bg-ink">
          <CityMapArt className="absolute inset-0 h-full w-full" />
          <div className="absolute left-4 top-4 flex flex-wrap gap-2">
            <Stamp tone="info" className="bg-ink">Simulation</Stamp>
            <Stamp tone="mute" className="bg-ink">Mock routing</Stamp>
          </div>
          <dl className="absolute bottom-4 left-4 grid gap-1.5 bg-ink/90 px-3 py-2 font-mono text-[11px] uppercase">
            <div className="flex items-center gap-2"><span className="inline-block h-3 w-3 bg-orange" />scene</div>
            <div className="flex items-center gap-2"><span className="inline-block h-3 w-3 bg-bone" />ambulance (simulated)</div>
            <div className="flex items-center gap-2"><span className="inline-block h-3 w-3 border border-red" />road closure, rerouted</div>
            <div className="flex items-center gap-2"><span className="inline-block h-3 w-3 bg-green" />green corridor (simulation)</div>
          </dl>
        </div>
      </section>

      <section aria-labelledby="flow" className="border-t hair px-5 py-10 sm:px-10">
        <h2 id="flow" className="rail-title mb-5 !text-base">How a call moves</h2>
        <ol className="grid gap-px bg-rule/30 sm:grid-cols-2 lg:grid-cols-6">
          {FLOW.map((f, i) => (
            <li key={f.t} className="relative bg-ink p-4">
              <div className="flex items-center justify-between">
                <FlowGlyph kind={f.g} className="h-10 w-10 text-orange" />
                <span className="font-mono text-xs text-rule">0{i + 1}</span>
              </div>
              <div className="mt-3 font-display text-2xl font-bold uppercase leading-none">{f.t}</div>
              <p className="mt-1 text-sm text-bone/70">{f.d}</p>
            </li>
          ))}
        </ol>
      </section>

      <footer className="border-t hair px-5 py-6 sm:px-10">
        <HazardBand height={8} dark className="mb-4" />
        <p className="font-mono text-xs uppercase text-rule">
          Prototype for research and demonstration. Not clinically validated. In a real emergency dial 112.
        </p>
        <p className="mt-1 text-xs text-rule">
          Map data © OpenStreetMap contributors. Routing: OSRM (optional) with a deterministic offline fallback. Maps: Leaflet. Optimisation: Google OR-Tools.
        </p>
      </footer>
    </div>
  )
}
