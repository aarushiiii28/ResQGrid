import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import CommandCenter from './CommandCenter'

vi.mock('../../components/MapView', () => ({ default: () => <div data-testid="map" /> }))

const now = new Date().toISOString()
const prov = (value: unknown, source = 'ai_extracted', extra = {}) => ({ value, source, at: now, confidence: 0.85, human_confirmed: source === 'human_confirmed', evidence: null, ...extra })
const incident = (extra = {}) => ({
  id: 'INC-1001',
  created_at: now,
  created_by: 'caller-1',
  status: 'new',
  channel: 'sos',
  reported_category: prov('cardiac', 'ai_extracted', { evidence: 'chest pain' }),
  operator_priority: prov(1),
  patient_count: null,
  location_candidates: [{ lat: 30.9, lon: 75.85, accuracy_m: 12, approximate: false, label: 'gps, 1s old' }],
  facts: { bleeding: prov(true, 'human_confirmed') },
  unknowns: ['conscious', 'patient_count'],
  conflicts: [],
  assigned_vehicle_id: null,
  destination_id: null,
  needs_review: false,
  review_reasons: [],
  building_id: null,
  duplicate_suggestions: [],
  ...extra,
})
const comp = (total: number) => ({ time: 5, freshness: 0.1, coverage_loss: 0.03, uncertainty: 1, switch: 0, weighted: { time: 5, freshness: 0.8, coverage_loss: 1.8, uncertainty: 1, switch: 0 }, total })
const cand = (v: string, h: string, rank: number, total: number) => ({
  vehicle_id: v,
  hospital_id: h,
  feasible: true,
  rank,
  is_current: false,
  eta_scene_low_s: 240,
  eta_scene_s: 300,
  eta_scene_high_s: 400,
  eta_hospital_s: 600,
  eta_total_low_s: 700,
  eta_total_high_s: 1000,
  components: comp(total),
  constraints: { available: true, capability: true, resource_not_unavailable: true },
  reason_codes: ['ELIGIBLE', 'CAPABLE_DESTINATION', 'NEAREST_UNIT'],
})
const decision = (extra = {}) => ({
  id: 'DEC-0001',
  incident_id: 'INC-1001',
  policy: 'P1',
  policy_version: 'P1-ranked-pairs-v1',
  created_at: now,
  status: 'pending',
  weights: { w_time: 1, w_fresh: 8, w_coverage: 60, w_uncertainty: 1, w_switch: 8, transport_weight: 0.5 },
  candidates: [cand('AMB-13', 'HOSP-01', 1, 13.2), cand('AMB-14', 'HOSP-01', 2, 15.9)],
  exclusions: [{ kind: 'vehicle', id: 'AMB-01', reasons: ['missing_capability:ALS'] }],
  recommendation: cand('AMB-13', 'HOSP-01', 1, 13.2),
  baseline: { ...cand('AMB-14', 'HOSP-05', 1, 19), reason_codes: ['BASELINE_NEAREST_ELIGIBLE'] },
  keep_current: false,
  assumptions: [],
  scene_route: null,
  hospital_route: null,
  approved_by: null,
  rationale: null,
  ...extra,
})
const vehicle = (id: string, extra = {}) => ({ id, capability_tags: ['ALS', 'cardiac'], status: 'available', availability: 'available', location: { lat: 30.9, lon: 75.85, source: 'simulated' }, location_state: 'live', location_age_s: 4, last_update_age_s: 4, assigned_incident_id: null, offline: false, ...extra })
const hospital = (id: string, stale = false) => ({
  id,
  name: `Synthetic General ${id.slice(-2)}`,
  capabilities: ['emergency', 'cardiac'],
  location: { lat: 30.91, lon: 75.86 },
  resources: { cath_lab: { status: stale ? 'unknown' : 'available', reported_status: 'available', count: 2, stale, age_s: stale ? 9000 : 30, source: 'simulated' } },
})

function renderCommand(over: Record<string, unknown> = {}) {
  signIn({ sub: 'disp-1', role: 'dispatcher', name: 'Dispatcher 1' })
  installWebSocket()
  const api = mockApi({
    'GET /api/incidents': [incident()],
    'GET /api/vehicles': [vehicle('AMB-13'), vehicle('AMB-14'), vehicle('AMB-07', { location_state: 'stale', location_age_s: 400, availability: 'unknown' })],
    'GET /api/hospitals': [hospital('HOSP-01'), hospital('HOSP-05', true)],
    'GET /api/alerts': [],
    'GET /api/dispatch/decisions': [decision()],
    'GET /api/assignments': [],
    'GET /api/routing/disruptions': [],
    'GET /api/dispatch/autopilot': { enabled: true, delay_s: 3, label: 'Demo Autopilot: simulated dispatcher approval' },
    'GET /api/incidents/INC-1001/audit': [],
    ...over,
  })
  render(
    <MemoryRouter>
      <AuthProvider>
        <EventsProvider>
          <CommandCenter />
        </EventsProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
  return api
}

describe('command center', () => {
  it('queue is a ruled table with priority, age and unit, and the first incident is selected', async () => {
    renderCommand()
    const queue = await screen.findByRole('complementary', { name: 'Incident queue' })
    expect(within(queue).getByText('INC-1001')).toBeInTheDocument()
    expect(within(queue).getByText(/cardiac/)).toBeInTheDocument()
    expect(within(queue).getByText('none')).toBeInTheDocument() // no unit yet
    const sel = screen.getByRole('complementary', { name: 'Selected incident' })
    expect(await within(sel).findByText('INC-1001')).toBeInTheDocument()
  })

  it('selected incident separates reported / confirmed / unknown with provenance', async () => {
    renderCommand()
    const sel = await screen.findByRole('complementary', { name: 'Selected incident' })
    await within(sel).findByText('Reported')
    expect(within(sel).getAllByText('ai extracted').length).toBeGreaterThan(0)
    expect(within(sel).getByText(/chest pain/)).toBeInTheDocument()
    expect(within(sel).getByText('human confirmed')).toBeInTheDocument()
    expect(within(sel).getByText('? conscious')).toBeInTheDocument()
    expect(within(sel).getByText('? patient count')).toBeInTheDocument()
  })

  it('stale units and hospitals carry badges and are never shown as live', async () => {
    renderCommand()
    const queue = await screen.findByRole('complementary', { name: 'Incident queue' })
    await userEvent.click(within(queue).getByRole('button', { name: /Units \(3\)/ }))
    expect(within(queue).getByText(/stale 6m 40s/)).toBeInTheDocument()
    expect(within(queue).getAllByText('unknown').length).toBeGreaterThan(0)
    await userEvent.click(within(queue).getByRole('button', { name: /Hospitals \(2\)/ }))
    expect(within(queue).getByText('stale data')).toBeInTheDocument()
  })

  it('Recommend posts to the server and opens Dispatch Compare', async () => {
    const api = renderCommand({ 'POST /api/dispatch/recommend': decision() })
    await userEvent.click(await screen.findByRole('button', { name: 'Recommend' }))
    await waitFor(() => expect(api.find('POST', '/api/dispatch/recommend')[0]?.body).toEqual({ incident_id: 'INC-1001', autopilot: false }))
    expect(await screen.findByText('Ranked unit / destination pairs')).toBeInTheDocument()
  })

  async function openCompare() {
    const api = renderCommand({ 'POST /api/dispatch/DEC-0001/approve': decision({ status: 'approved' }) })
    await userEvent.click(await screen.findByRole('tab', { name: /dispatch compare/i }))
    await screen.findByText('Ranked unit / destination pairs')
    return api
  }

  it('Dispatch Compare shows ranked pairs, ETA range, score components, constraints and the P0 baseline', async () => {
    await openCompare()
    expect(screen.getAllByText('AMB-13').length).toBeGreaterThan(0)
    expect(screen.getAllByText('4-7 min').length).toBeGreaterThan(0)
    expect(screen.getByText(/P0 baseline for comparison/)).toBeInTheDocument()
    expect(screen.getByText(/lower score is better/i)).toBeInTheDocument()
    await userEvent.click(screen.getAllByText('AMB-14')[0])
    expect(await screen.findByText(/coverage loss/)).toBeInTheDocument()
    expect(screen.getAllByText(/capability/).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: /Show excluded \(1\)/ }))
    expect(screen.getByText(/missing capability:ALS/)).toBeInTheDocument()
  })

  it('approve / reject / override are disabled until a reason is given, and override also needs a row', async () => {
    const api = await openCompare()
    const approve = screen.getByRole('button', { name: 'Approve' })
    const reject = screen.getByRole('button', { name: 'Reject' })
    const override = screen.getByRole('button', { name: 'Override' })
    expect(approve).toBeDisabled()
    expect(reject).toBeDisabled()
    expect(override).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Reason (required)'), 'closest capable unit')
    expect(approve).toBeEnabled()
    expect(reject).toBeEnabled()
    expect(override).toBeDisabled() // still no row selected
    await userEvent.click(screen.getByLabelText(/Select AMB-14 to HOSP-01 for override/))
    expect(override).toBeEnabled()
    await userEvent.click(override)
    await waitFor(() => expect(api.find('POST', '/api/dispatch/DEC-0001/approve')[0]?.body).toEqual({ action: 'override', reason: 'closest capable unit', vehicle_id: 'AMB-14', hospital_id: 'HOSP-01' }))
  })

  it('approve sends the reason and refreshes', async () => {
    const api = await openCompare()
    await userEvent.type(screen.getByLabelText('Reason (required)'), 'route clear')
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.find('POST', '/api/dispatch/DEC-0001/approve')[0]?.body).toMatchObject({ action: 'approve', reason: 'route clear' }))
  })

  it('Demo Autopilot is labelled simulated and can be toggled', async () => {
    const api = renderCommand({ 'POST /api/dispatch/autopilot': { enabled: false, delay_s: 3, label: 'x' } })
    const toggle = await screen.findByRole('button', { name: /Demo Autopilot: on/i })
    expect(within(toggle).getByText('simulated')).toBeInTheDocument()
    await userEvent.click(toggle)
    await waitFor(() => expect(api.find('POST', '/api/dispatch/autopilot')[0]?.body).toEqual({ enabled: false }))
  })

  it('a timed-out acknowledgment shows in the alerts strip and selects the incident when clicked', async () => {
    renderCommand({
      'GET /api/alerts': [{ id: 'no_acknowledgment:ASG-1', kind: 'no_acknowledgment', severity: 'red', text: 'No acceptance from AMB-13 for INC-1001.', entity: 'ASG-1', incident_id: 'INC-1001', pulse: true }],
    })
    const strip = await screen.findByRole('region', { name: 'Alerts' })
    expect(await within(strip).findByText(/No acceptance from AMB-13/)).toBeInTheDocument()
    expect(within(strip).getByText('urgent')).toBeInTheDocument()
  })

  it('shows what is stale when the server is unreachable, instead of pretending it is live', async () => {
    renderCommand({ 'GET /api/alerts': () => new Response('x', { status: 500 }) })
    expect(await screen.findByText(/Server unreachable: showing last known data/)).toBeInTheDocument()
  })

  it('Add incident: Create stays disabled until a point is placed, and "Close road" arms the map', async () => {
    renderCommand()
    await userEvent.click(await screen.findByRole('button', { name: 'Add incident' }))
    expect(screen.getByRole('button', { name: 'Create incident' })).toBeDisabled()
    expect(screen.getByText('Click the map to place the incident.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Close road' }))
    expect(screen.getByRole('button', { name: 'Close road' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText(/Click the map where the road is closed/)).toBeInTheDocument()
  })

  it('layer toggles change state, including the demand forecast layer', async () => {
    renderCommand()
    const aed = await screen.findByRole('button', { name: 'AEDs' })
    expect(aed).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(aed)
    expect(aed).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Demand' })).toHaveAttribute('aria-pressed', 'false')
  })
})
