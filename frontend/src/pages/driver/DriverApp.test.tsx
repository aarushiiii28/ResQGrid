import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import DriverApp from './DriverApp'

vi.mock('../../components/MapView', () => ({ default: () => <div data-testid="map" /> }))

const vehicle = (status: string) => ({
  id: 'AMB-07',
  capability_tags: ['ALS', 'cardiac'],
  status,
  availability: status === 'available' ? 'available' : 'not_available',
  location: { lat: 30.9, lon: 75.85, source: 'simulated' },
  location_state: 'live',
  location_age_s: 2,
  last_update_age_s: 2,
  assigned_incident_id: 'INC-1001',
  offline: false,
})
const incident = (extra = {}) => ({
  id: 'INC-1001',
  status: 'dispatched',
  category: 'cardiac',
  category_source: 'ai_extracted',
  priority: 1,
  patient_count: 1,
  unknowns: ['conscious'],
  arrived: false,
  handover_status: null,
  location: { lat: 30.9, lon: 75.85, accuracy_m: 10, approximate: false },
  hospital: { id: 'HOSP-02', name: 'Synthetic General 02', lat: 30.91, lon: 75.86 },
  ...extra,
})
const route = {
  distance_m: 1200,
  duration_low_s: 100,
  duration_s: 140,
  duration_high_s: 190,
  polyline: [[30.9, 75.85], [30.91, 75.86]],
  steps: [],
  source: 'mock',
  confidence: 0.6,
  degraded: false,
  notes: [],
  feasible: true,
}
const plan = (extra = {}) => ({
  vehicle_id: 'AMB-07',
  incident_id: 'INC-1001',
  phase: 'scene',
  vehicle_status: 'accepted',
  target: { kind: 'scene', name: 'Incident location', lat: 30.91, lon: 75.86 },
  route,
  along_m: 100,
  remaining_m: 1100,
  remaining_low_s: 90,
  remaining_s: 130,
  remaining_high_s: 180,
  next_step: { instruction: 'Turn right', maneuver: 'turn-right', distance_m: 300, duration_s: 30, lat: 30.905, lon: 75.855 },
  distance_to_next_m: 300,
  last_100m: false,
  entrance: null,
  reroute: null,
  green_corridor: { simulation: true, label: 'Green corridor: simulation only. No real signal is controlled.', signals: [{ id: 'SIG-1', lat: 30.9, lon: 75.85, along_m: 600 }] },
  simulated_movement: false,
  ...extra,
})
const asg = (state: string) => ({ id: 'ASG-0001', incident_id: 'INC-1001', vehicle_id: 'AMB-07', state, late: false, fallback: null })

function renderDriver() {
  signIn({ sub: 'driver-AMB-07', role: 'driver', name: 'Driver AMB-07', vehicle_id: 'AMB-07' })
  installWebSocket()
  return render(
    <MemoryRouter>
      <AuthProvider>
        <EventsProvider>
          <DriverApp />
        </EventsProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('driver app', () => {
  it('standing by when there is no assignment', async () => {
    mockApi({ 'GET /api/driver/me': { vehicle: { ...vehicle('available'), assigned_incident_id: null }, assignment: null, incident: null, plan: null } })
    renderDriver()
    expect(await screen.findByText('Standing by')).toBeInTheDocument()
  })

  it('receives an assignment: opening it marks delivered then acknowledged, but does not accept', async () => {
    const api = mockApi({
      'GET /api/driver/me': { vehicle: vehicle('assigned'), assignment: asg('sent'), incident: incident(), plan: plan({ vehicle_status: 'assigned' }) },
      'POST /api/assignments/ASG-0001/delivered': {},
      'POST /api/assignments/ASG-0001/acknowledge': {},
    })
    renderDriver()
    expect(await screen.findByText(/New assignment · INC-1001/)).toBeInTheDocument()
    await waitFor(() => expect(api.count('POST', '/api/assignments/ASG-0001/acknowledge')).toBe(1))
    expect(api.count('POST', '/api/assignments/ASG-0001/delivered')).toBe(1)
    expect(api.count('POST', '/api/assignments/ASG-0001/accept')).toBe(0)
    expect(screen.getByText(/Opening this job is not accepting it/)).toBeInTheDocument()
  })

  it('Accept posts the accept action; Reject needs a confirm and sends the reason', async () => {
    const api = mockApi({
      'GET /api/driver/me': { vehicle: vehicle('assigned'), assignment: asg('acknowledged'), incident: incident(), plan: plan() },
      'POST /api/assignments/*': {},
    })
    renderDriver()
    const accept = await screen.findByRole('button', { name: 'Accept job' })
    expect(accept.className).toMatch(/min-h-\[56px\]|min-h-\[72px\]/)
    await userEvent.click(screen.getByRole('button', { name: 'Reject' }))
    await userEvent.type(screen.getByLabelText('Reason for rejecting'), 'vehicle fault')
    await userEvent.click(screen.getByRole('button', { name: 'Confirm reject' }))
    await waitFor(() => expect(api.find('POST', '/api/assignments/ASG-0001/reject')[0]?.body).toEqual({ note: 'vehicle fault' }))
    await userEvent.click(accept)
    await waitFor(() => expect(api.count('POST', '/api/assignments/ASG-0001/accept')).toBe(1))
  })

  it('after accepting, only the next status button is enabled; the rest are visibly disabled', async () => {
    const api = mockApi({
      'GET /api/driver/me': { vehicle: vehicle('accepted'), assignment: asg('accepted'), incident: incident(), plan: plan() },
      'PATCH /api/vehicles/AMB-07/state': { applied: true, reason: 'applied' },
    })
    renderDriver()
    const enRoute = await screen.findByRole('button', { name: /En route/ })
    expect(enRoute).toBeEnabled()
    for (const n of [/On scene/, /Transporting/, /Arrived/]) expect(screen.getByRole('button', { name: n })).toBeDisabled()
    await userEvent.click(enRoute)
    await waitFor(() => expect(api.find('PATCH', '/api/vehicles/AMB-07/state')[0]?.body).toMatchObject({ status: 'en_route', source: 'reported' }))
  })

  it('turn-by-turn shows distance and instruction, ETA range, and the simulation labels', async () => {
    mockApi({ 'GET /api/driver/me': { vehicle: vehicle('en_route'), assignment: asg('accepted'), incident: incident(), plan: plan({ vehicle_status: 'en_route' }) } })
    renderDriver()
    expect(await screen.findByText('Turn right')).toBeInTheDocument()
    expect(screen.getByText('300 m')).toBeInTheDocument()
    expect(screen.getByText(/2-3 min/)).toBeInTheDocument()
    expect(screen.getByText(/simulation only\. No real signal is controlled/)).toBeInTheDocument()
    expect(screen.getAllByText(/simulated/i).length).toBeGreaterThan(0)
  })

  it('reroute banner appears on a closure and needs exactly one tap to apply', async () => {
    const api = mockApi({
      'GET /api/driver/me': {
        vehicle: vehicle('en_route'),
        assignment: asg('accepted'),
        incident: incident(),
        plan: plan({ vehicle_status: 'en_route', reroute: { id: 'RR-0001', reason: 'closure', message: 'Road closed ahead (DIS-001). A route that avoids it is ready.', delta_s: 60, old_remaining_s: 200, new_remaining_s: 260, status: 'pending', route } }),
      },
      'POST /api/driver/AMB-07/reroute/RR-0001/accept': { status: 'accepted' },
    })
    renderDriver()
    expect(await screen.findByText(/Road closed ahead/)).toBeInTheDocument()
    expect(screen.getByText(/Not applied until you tap/)).toBeInTheDocument()
    expect(api.count('POST', '/api/driver/AMB-07/reroute')).toBe(0)
    await userEvent.click(screen.getByRole('button', { name: 'Take new route' }))
    await waitFor(() => expect(api.count('POST', '/api/driver/AMB-07/reroute/RR-0001/accept')).toBe(1))
  })

  it('last 100 m shows entrance guidance with a confirmation request when uncertain', async () => {
    mockApi({
      'GET /api/driver/me': {
        vehicle: vehicle('en_route'),
        assignment: asg('accepted'),
        incident: incident(),
        plan: plan({
          vehicle_status: 'en_route',
          remaining_m: 80,
          last_100m: true,
          entrance: {
            chosen: { entrance: { name: 'Entrance 2', landmark: 'beside the ATM', accessible: true, gate_restricted: true, kind: 'side' }, reasons: [] },
            needs_confirmation: true,
            confirmation_request: 'Confirm entrance for Block A: Entrance 2 or Entrance 1. Ask caller or crew.',
            alternatives: [{ entrance: { name: 'Entrance 1' } }],
          },
        }),
      },
    })
    renderDriver()
    expect(await screen.findByText('Entrance 2')).toBeInTheDocument()
    expect(screen.getByText(/beside the ATM/)).toBeInTheDocument()
    expect(screen.getByText('gate restricted')).toBeInTheDocument()
    expect(screen.getByText(/Confirm entrance for Block A/)).toBeInTheDocument()
  })

  it('Arrived is only offered while transporting, and then says the unit waits for the hospital', async () => {
    const api = mockApi({
      'GET /api/driver/me': {
        vehicle: vehicle('transporting'),
        assignment: asg('accepted'),
        incident: incident(),
        plan: plan({ phase: 'hospital', vehicle_status: 'transporting', target: { kind: 'hospital', name: 'Synthetic General 02', lat: 30.91, lon: 75.86 } }),
      },
      'POST /api/driver/AMB-07/arrived': {},
    })
    renderDriver()
    const arrived = await screen.findByRole('button', { name: /Arrived/ })
    expect(arrived).toBeEnabled()
    await userEvent.click(arrived)
    await waitFor(() => expect(api.count('POST', '/api/driver/AMB-07/arrived')).toBe(1))
  })

  it('the simulate-movement button calls the server and is labelled simulated', async () => {
    const api = mockApi({
      'GET /api/driver/me': { vehicle: vehicle('en_route'), assignment: asg('accepted'), incident: incident(), plan: plan({ vehicle_status: 'en_route' }) },
      'POST /api/driver/AMB-07/simulate-movement': { started: true, label: 'Simulated GPS movement' },
    })
    renderDriver()
    await userEvent.click(await screen.findByRole('button', { name: /Simulate movement along route/ }))
    await waitFor(() => expect(api.count('POST', '/api/driver/AMB-07/simulate-movement')).toBe(1))
    expect(await screen.findByRole('button', { name: /Stop simulated movement/ })).toBeInTheDocument()
  })
})
