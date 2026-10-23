import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, setToken } from './api'
import { lockSigner, unlockSigner } from './keys'
import type { Role, User } from './types'

/** Unlock this user's device key once (no prompt per action) and make sure the server knows its PUBLIC key.
 * Any failure leaves actions unsigned; the app keeps working. */
async function enableSigning(userId: string) {
  const k = await unlockSigner(userId)
  if (!k) return
  try {
    const mine = await api.get<{ key_id: string; revoked_at: string | null }[]>('/api/keys/me')
    if (!mine.some((m) => m.key_id === k.keyId && !m.revoked_at)) await api.post('/api/keys/register', { public_key: k.publicKey })
  } catch {
    /* the server will say so on the first signed action */
  }
}

const KEY = 'resqgrid.session'

interface Session {
  token: string
  user: User
}

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return null
    const s = JSON.parse(raw) as Session
    setToken(s.token)
    return s
  } catch {
    return null
  }
}

function save(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s))
    else sessionStorage.removeItem(KEY)
  } catch {
    /* storage unavailable: session lasts until reload */
  }
}

interface AuthCtx {
  user: User | null
  token: string | null
  login: (opts: { role?: Role; userId?: string }) => Promise<User>
  logout: () => void
}

const Ctx = createContext<AuthCtx>(null as unknown as AuthCtx)
export const useAuth = () => useContext(Ctx)

export const HOME: Record<Role, string> = {
  caller: '/caller',
  dispatcher: '/command',
  admin: '/command',
  driver: '/driver',
  hospital: '/hospital',
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(load)

  const login = useCallback(async (opts: { role?: Role; userId?: string }) => {
    const r = await api.post<{ token: string; user: User }>('/api/auth/demo-login', {
      role: opts.role,
      user_id: opts.userId,
    })
    setToken(r.token)
    const s = { token: r.token, user: r.user }
    save(s)
    setSession(s)
    void enableSigning(r.user.sub)
    return r.user
  }, [])

  // a reloaded tab keeps its session but not the unlocked key: unlock it again
  useEffect(() => {
    if (session) void enableSigning(session.user.sub)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const logout = useCallback(() => {
    lockSigner()
    setToken(null)
    save(null)
    setSession(null)
  }, [])

  const value = useMemo(
    () => ({ user: session?.user ?? null, token: session?.token ?? null, login, logout }),
    [session, login, logout],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
