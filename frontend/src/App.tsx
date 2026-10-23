import { lazy, Suspense, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import TopBar, { DemoStrip } from './components/Shell'
import { Loading } from './components/ui'
import { HOME, useAuth } from './lib/auth'
import type { Role } from './lib/types'
import Landing from './pages/Landing'

const CallerPage = lazy(() => import('./pages/caller/CallerPage'))
const CommandCenter = lazy(() => import('./pages/command/CommandCenter'))
const DriverApp = lazy(() => import('./pages/driver/DriverApp'))
const HospitalConsole = lazy(() => import('./pages/hospital/HospitalConsole'))
const AuditPage = lazy(() => import('./pages/audit/AuditPage'))
const RiskPage = lazy(() => import('./pages/risks/RiskPage'))
const MciPage = lazy(() => import('./pages/mci/MciPage'))
const ReplayLab = lazy(() => import('./pages/replay/ReplayLab'))
const LedgerPage = lazy(() => import('./pages/ledger/LedgerPage'))
const VerifyReceiptPage = lazy(() => import('./pages/ledger/VerifyReceiptPage'))

function Guard({ roles, children }: { roles: Role[]; children: ReactNode }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/" replace />
  if (!roles.includes(user.role)) return <Navigate to={HOME[user.role]} replace />
  return <>{children}</>
}

export default function App() {
  const { pathname } = useLocation()
  const caller = pathname.startsWith('/caller')
  return (
    <div className={`flex h-full flex-col ${caller ? 'bg-paper text-ink' : 'bg-ink text-bone'}`}>
      <DemoStrip />
      <TopBar />
      <main className="min-h-0 flex-1 overflow-auto">
        <Suspense fallback={<Loading what="Loading screen" />}>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/caller" element={<Guard roles={['caller']}><CallerPage /></Guard>} />
            <Route path="/command" element={<Guard roles={['dispatcher', 'admin']}><CommandCenter /></Guard>} />
            <Route path="/driver" element={<Guard roles={['driver']}><DriverApp /></Guard>} />
            <Route path="/hospital" element={<Guard roles={['hospital']}><HospitalConsole /></Guard>} />
            <Route path="/risks" element={<Guard roles={['dispatcher', 'admin']}><RiskPage /></Guard>} />
            <Route path="/mci" element={<Guard roles={['dispatcher', 'admin']}><MciPage /></Guard>} />
            <Route path="/replay" element={<Guard roles={['dispatcher', 'admin']}><ReplayLab /></Guard>} />
            <Route path="/ledger" element={<Guard roles={['dispatcher', 'admin']}><LedgerPage /></Guard>} />
            <Route path="/verify-receipt" element={<Guard roles={['caller', 'dispatcher', 'admin', 'driver', 'hospital']}><VerifyReceiptPage /></Guard>} />
            <Route path="/audit" element={<Guard roles={['dispatcher', 'admin']}><AuditPage /></Guard>} />
            <Route path="/audit/:incidentId" element={<Guard roles={['dispatcher', 'admin']}><AuditPage /></Guard>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  )
}
