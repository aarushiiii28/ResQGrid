import { useCallback, useEffect, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { HOME, useAuth } from '../lib/auth'
import { useEvents, useLiveRefresh } from '../lib/events'
import type { Role } from '../lib/types'
import { Octagon } from './art'
import { Stamp } from './ui'

export function DemoStrip() {
  return (
    <div role="note" className="flex min-h-[30px] items-center justify-center gap-3 bg-orange px-3 py-1 text-ink">
      <span className="font-mono text-xs font-bold uppercase tracking-wider">DEMO / SIMULATED</span>
      <span className="hidden font-mono text-xs uppercase tracking-wider sm:inline">Synthetic data only. No real emergency service is contacted.</span>
    </div>
  )
}

export function LinkBadge() {
  const { link, lastSeq } = useEvents()
  const map = {
    live: { t: 'ok', l: 'LIVE' },
    connecting: { t: 'mute', l: 'CONNECTING' },
    reconnecting: { t: 'warn', l: 'RECONNECTING' },
    offline: { t: 'bad', l: 'OFFLINE' },
  } as const
  const m = map[link]
  return (
    <Stamp tone={m.t} title={`Realtime link. Last event #${lastSeq}. After a drop the board resyncs from the server.`}>
      <span className={`mr-1.5 inline-block h-1.5 w-1.5 ${link === 'live' ? 'bg-green' : link === 'offline' ? 'bg-red' : 'bg-amber blink'}`} aria-hidden="true" />
      {m.l}
    </Stamp>
  )
}

const OPS_NAV = [
  { to: '/command', label: 'Command' },
  { to: '/risks', label: 'Risks' },
  { to: '/mci', label: 'Mass casualty' },
  { to: '/replay', label: 'Replay lab' },
  { to: '/audit', label: 'Passport' },
  { to: '/ledger', label: 'Integrity' },
  { to: '/verify-receipt', label: 'Receipts' },
]
const NAV: Record<Role, { to: string; label: string; disabled?: boolean }[]> = {
  caller: [{ to: '/verify-receipt', label: 'Receipts' }],
  dispatcher: OPS_NAV,
  admin: OPS_NAV,
  driver: [],
  hospital: [],
}

function useOpenRisks(enabled: boolean) {
  const [n, setN] = useState(0)
  const load = useCallback(() => {
    if (!enabled) return
    api.get<{ status: string }[]>('/api/risks').then((r) => setN(r.filter((x) => x.status === 'open' || x.status === 'awaiting').length)).catch(() => {})
  }, [enabled])
  useLiveRefresh(load, { pollMs: 8000, filter: (e) => e.event_type.startsWith('risk.') })
  return n
}

interface DemoUser {
  sub: string
  role: Role
  name: string
  vehicle_id?: string | null
  hospital_id?: string | null
}

export function Logo({ size = 30 }: { size?: number }) {
  return (
    <span className="flex items-center gap-2.5">
      <Octagon size={size} fill="#E4570F" stroke="#ECE6D8">
        <path d={`M${size * 0.43} ${size * 0.22}h${size * 0.14}v${size * 0.21}h${size * 0.21}v${size * 0.14}h-${size * 0.21}v${size * 0.21}h-${size * 0.14}v-${size * 0.21}h-${size * 0.21}v-${size * 0.14}h${size * 0.21}z`} fill="#15120E" />
      </Octagon>
      <span className="font-display text-[26px] font-bold uppercase leading-none tracking-wider">ResQGrid</span>
    </span>
  )
}

export default function TopBar() {
  const { user, login, logout } = useAuth()
  const nav = useNavigate()
  const loc = useLocation()
  const [users, setUsers] = useState<DemoUser[]>([])
  const openRisks = useOpenRisks(user?.role === 'dispatcher' || user?.role === 'admin')
  useEffect(() => {
    api.get<DemoUser[]>('/api/auth/users').then(setUsers).catch(() => setUsers([]))
  }, [])
  if (!user) return null
  const units = users.filter((u) => u.role === 'driver')
  const hospitals = users.filter((u) => u.role === 'hospital')
  const switchTo = async (opts: { role?: Role; userId?: string }) => {
    const u = await login(opts)
    nav(HOME[u.role])
  }
  const onCaller = loc.pathname.startsWith('/caller')
  return (
    <header className={`flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-3 py-2 ${onCaller ? 'border-ink bg-paper text-ink' : 'hair bg-ink text-bone'}`}>
      <Link to="/" aria-label="ResQGrid home" className="shrink-0">
        <Logo />
      </Link>
      <nav aria-label="Sections" className="flex gap-1">
        {NAV[user.role].map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `label px-3 py-1.5 text-lg ${isActive ? 'bg-bone text-ink' : 'text-bone hover:bg-bone/10'}`}>
            {n.label}
            {n.to === '/risks' && openRisks > 0 && <span aria-label={` open risks`} className="ml-1.5 inline-block min-w-[20px] bg-red px-1 text-center font-mono text-xs text-bone">{openRisks}</span>}
          </NavLink>
        ))}
      </nav>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {!onCaller && <LinkBadge />}
        <label className="flex items-center gap-1.5 font-mono text-[11px] uppercase">
          <span className={onCaller ? 'text-ink/70' : 'text-rule'}>Role</span>
          <select
            aria-label="Switch role"
            value={user.role}
            onChange={(e) => switchTo({ role: e.target.value as Role })}
            className={`min-h-[36px] border px-2 font-mono text-xs uppercase ${onCaller ? 'border-ink bg-paper text-ink' : 'border-rule/60 bg-ink text-bone'}`}
          >
            {(['caller', 'dispatcher', 'driver', 'hospital', 'admin'] as Role[]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        {user.role === 'driver' && units.length > 0 && (
          <select aria-label="Select unit" value={user.sub} onChange={(e) => switchTo({ userId: e.target.value })} className="min-h-[36px] border border-rule/60 bg-ink px-2 font-mono text-xs uppercase text-bone">
            {units.map((u) => (
              <option key={u.sub} value={u.sub}>
                {u.vehicle_id}
              </option>
            ))}
          </select>
        )}
        {user.role === 'hospital' && hospitals.length > 0 && (
          <select aria-label="Select hospital" value={user.sub} onChange={(e) => switchTo({ userId: e.target.value })} className="min-h-[36px] border border-rule/60 bg-ink px-2 font-mono text-xs uppercase text-bone">
            {hospitals.map((u) => (
              <option key={u.sub} value={u.sub}>
                {u.hospital_id}
              </option>
            ))}
          </select>
        )}
        <button
          className={`min-h-[36px] border px-3 font-mono text-xs uppercase ${onCaller ? 'border-ink hover:bg-ink hover:text-paper' : 'border-rule/60 hover:bg-bone hover:text-ink'}`}
          onClick={() => {
            logout()
            nav('/')
          }}
        >
          Sign out
        </button>
      </div>
    </header>
  )
}
