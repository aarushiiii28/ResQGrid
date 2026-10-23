import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import DriverApp from './DriverApp'

const maps: { polylines: number }[] = []
vi.mock('../../components/MapView', () => ({ default: ({ children }: { children?: unknown }) => { maps.push({ polylines: 0 }); return <div data-testid="map">{children as never}</div> } }))
vi.mock('react-leaflet', async (orig) => {
  const real = (await orig()) as Record<string, unknown>
  return { ...real, Polyline: ({ pathOptions }: { pathOptions?: { color?: string; opacity?: number } }) => <i data-testid="polyline" data-color={pathOptions?.color} data-opacity={pathOptions?.opacity ?? 1} />, Marker: () => null, Tooltip: () => null }
})

const vehicle = { id: 'AMB-07', capability_tags: ['ALS'], status: 'en_route', availability: 'not_available', location: { lat: 30.9, lon: 75.85, source: 'simulated' }, location_state: 'live', location_age_s: 2, last_update_age_s: 2, assigned_incident_id: 'INC-1001', offline: false }
const incident = { id: 'INC-1001', status: 'dispatched', category: 'cardiac', category_source: 'reported', priority: 1, patient_count: 1, unknowns: [], arrived: false, handover_status: null, location: { lat: 30.9, lon: 75.85, accuracy_m: 10, approximate: false }, hospital: { id: 'HOSP-02', name: 'Synthetic General 02', lat: 30.91, lon: 75.86 } }
const route = { distance_m: 1200, duration_low_s: 100, duration_s: 140, duration_high_s: 190, polyline: [[30.9, 75.85], [30.91, 75.86]], steps: [], source: 'mock', confidence: 0.6, degraded: false, notes: [], feasible: true }
const notice = (extra = {}) => ({ id: 'RN-0001', kind: 'rejoin', reason: 'wrong_turn', message: 'Off route. Rejoin in 400 m.', saved_s: -42, old_remaining_s: 200, new_remaining_s: 242, reason_codes: ['OFF_ROUTE_CONFIRMED'], at: new Date().toISOString(), acknowledged: false, ...extra })
const plan = (extra = {}) => ({
  vehicle_id: 'AMB-07', incident_id: 'INC-1001', phase: 'scene', vehicle_status: 'en_route', target: { kind: 'scene', name: 'Incident location', lat: 30.91, lon: 75.86 }, route,
  along_m: 100, remaining_m: 1100, remaining_low_s: 90, remaining_s: 130, remaining_high_s: 180, next_step: null, distance_to_next_m: null, last_100m: false, entrance: null, reroute: null,
  green_corridor: { simulation: true, label: 'Green corridor: simulation only.', signals: [] }, simulated_movement: true,
  route_lock: { locked: true, version: 1, reroutes: 0, policy_version: 'stable-v1', committed_at: new Date().toISOString() }, route_notice: null, off_route: null, backups: [], ...extra,
})
const asg = { id: 'ASG-0001', incident_id: 'INC-1001', vehicle_id: 'AMB-07', state: 'accepted', late: false, fallback: null }

function renderDriver(p: object, extra: Record<string, unknown> = {}) {
  signIn({ sub: 'driver-AMB-07', role: 'driver', name: 'Driver AMB-07', vehicle_id: 'AMB-07' })
  installWebSocket()
  const api = mockApi({ 'GET /api/driver/me': { vehicle, assignment: asg, incident, plan: p }, ...extra })
  render(<MemoryRouter><AuthProvider><EventsProvider><DriverApp /></EventsProvider></AuthProvider></MemoryRouter>)
  return api
}

describe('driver: stable route', () => {
  it('shows ROUTE LOCKED with the number of changes and the policy version', async () => {
    renderDriver(plan({ route_lock: { locked: true, version: 3, reroutes: 2, policy_version: 'stable-v1', committed_at: new Date().toISOString() } }))
    const bar = await screen.findByLabelText('Route status')
    expect(within(bar).getByText('route locked')).toBeInTheDocument()
    expect(bar).toHaveTextContent('changes 2 · v3 · stable-v1')
  })

  it('an automatic route change shows the reason and minutes, and needs only a one-tap acknowledge (not an approval)', async () => {
    const api = renderDriver(plan({ route_notice: notice({ kind: 'reroute', reason: 'congestion', message: 'Heavy traffic ahead. New route is 2.1 min faster.', saved_s: 126 }) }),
      { 'POST /api/driver/AMB-07/route-notice/RN-0001/ack': { acknowledged: true } })
    const banner = await screen.findByLabelText('Route changed')
    expect(banner).toHaveTextContent('Heavy traffic ahead. New route is 2.1 min faster.')
    expect(banner).toHaveTextContent('2.1 min saved')
    expect(banner).toHaveTextContent('Same destination')
    expect(banner).toHaveTextContent('The new route is already active')
    expect(screen.queryByRole('button', { name: 'Take new route' })).toBeNull()
    const btn = within(banner).getByRole('button', { name: 'Got it' })
    expect(btn.className).toMatch(/btn-touch/)
    await userEvent.click(btn)
    await waitFor(() => expect(api.count('POST', '/api/driver/AMB-07/route-notice/RN-0001/ack')).toBe(1))
  })

  it('a necessary but longer route says so honestly', async () => {
    renderDriver(plan({ route_notice: notice({ reason: 'blocked', kind: 'reroute', message: 'Road closed ahead. New route avoids it, 1.4 min longer.', saved_s: -84 }) }))
    expect(await screen.findByLabelText('Route changed')).toHaveTextContent('1.4 min longer, but necessary')
  })

  it('an acknowledged notice is not shown again', async () => {
    renderDriver(plan({ route_notice: notice({ acknowledged: true }) }))
    await screen.findByLabelText('Route status')
    expect(screen.queryByLabelText('Route changed')).toBeNull()
  })

  it('off-route banner: checking, off route, and GPS uncertain are different states with text', async () => {
    renderDriver(plan({ off_route: { state: 'off_route', deviation_m: 183, message: 'Off route. Finding the quickest way back…', rejoin_in_m: null, via: null } }))
    const b = await screen.findByLabelText('Off route')
    expect(b).toHaveTextContent('off route')
    expect(b).toHaveTextContent('183 m from the route')
    expect(b).toHaveTextContent('Finding the quickest way back')
  })

  it('poor GPS says no change will be made', async () => {
    renderDriver(plan({ off_route: { state: 'uncertain', deviation_m: 300, message: 'GPS accuracy is poor. The route will not be changed until the signal improves.', rejoin_in_m: null, via: null } }))
    const b = await screen.findByLabelText('Off route')
    expect(b).toHaveTextContent('gps uncertain')
    expect(b).toHaveTextContent('will not be changed')
  })

  it('backup routes are drawn dim and only when the server says they are relevant; the committed route is one solid line', async () => {
    renderDriver(plan({ backups: [{ id: 'BK-1', polyline: [[30.9, 75.85], [30.9, 75.86], [30.91, 75.86]], duration_s: 150, feasible: true, label: 'backup 1' }] }))
    await screen.findByLabelText('Route status')
    const lines = screen.getAllByTestId('polyline')
    const solid = lines.filter((l) => l.getAttribute('data-color') === '#E4570F')
    const dim = lines.filter((l) => l.getAttribute('data-color') === '#BDB5A3')
    expect(solid).toHaveLength(1)
    expect(dim).toHaveLength(1)
    expect(Number(dim[0].getAttribute('data-opacity'))).toBeLessThan(0.7)
  })

  it('no backups are drawn when none are relevant', async () => {
    renderDriver(plan({ backups: [] }))
    await screen.findByLabelText('Route status')
    expect(screen.getAllByTestId('polyline').filter((l) => l.getAttribute('data-color') === '#BDB5A3')).toHaveLength(0)
  })

  it('Simulate a wrong turn is a labelled demo control and posts to the simulate endpoint', async () => {
    const api = renderDriver(plan(), { 'POST /api/driver/AMB-07/simulate-movement': {}, 'POST /api/driver/AMB-07/simulate-wrong-turn': { simulated: true } })
    await userEvent.click(await screen.findByRole('button', { name: /Simulate movement along route/ }))
    const btn = await screen.findByRole('button', { name: 'Simulate a wrong turn' })
    expect(btn).toHaveAttribute('title', expect.stringMatching(/Demo only/))
    expect(btn.className).toMatch(/btn-touch/)
    await userEvent.click(btn)
    await waitFor(() => expect(api.count('POST', '/api/driver/AMB-07/simulate-wrong-turn')).toBe(1))
  })
})
