import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import HospitalConsole from './HospitalConsole'

const now = new Date().toISOString()
const pa = (extra: Record<string, unknown> = {}) => ({
  id: 'PA-0001',
  incident_id: 'INC-1001',
  hospital_id: 'HOSP-02',
  vehicle_id: 'AMB-07',
  created_at: now,
  reported_reason: 'Cardiac (ai extracted)',
  reported_category: 'cardiac',
  confirmed_observations: [{ key: 'bleeding', value: true, source: 'human_confirmed', at: now }],
  reported_observations: [{ key: 'conscious', value: false, source: 'ai_extracted', at: now }],
  unknowns: ['Number of patients not known'],
  eta_low_s: 360,
  eta_high_s: 480,
  status: 'incoming',
  acknowledged_at: null,
  arrived_at: null,
  arrival_source: null,
  handover_started_at: null,
  handover_completed_at: null,
  handover_duration_s: null,
  superseded_reason: null,
  checklist: [
    { key: 'patient_count', label: 'Number of patients not known', status: 'open', note: null },
    { key: 'age', label: 'Age (not collected in this prototype)', status: 'open', note: null },
  ],
  ...extra,
})
const hospital = {
  id: 'HOSP-02',
  name: 'Synthetic General 02',
  capabilities: ['emergency', 'cardiac', 'stroke'],
  location: { lat: 30.9, lon: 75.85 },
  resources: {
    cath_lab: { status: 'unknown', reported_status: 'available', count: 2, stale: true, age_s: 7200, source: 'simulated' },
    er_bay: { status: 'available', reported_status: 'available', count: 3, stale: false, age_s: 30, source: 'human_confirmed' },
  },
}

function renderConsole(list: unknown[], extra: Record<string, unknown> = {}) {
  signIn({ sub: 'hosp-HOSP-02', role: 'hospital', name: 'HOSP-02 desk', hospital_id: 'HOSP-02' })
  installWebSocket()
  const api = mockApi({
    'GET /api/prealerts': () => list,
    'GET /api/hospitals': [hospital],
    'POST /api/prealerts/*': {},
    'PATCH /api/hospitals/HOSP-02/resources': { applied: true, reason: 'applied' },
    ...extra,
  })
  render(
    <MemoryRouter>
      <AuthProvider>
        <EventsProvider>
          <HospitalConsole />
        </EventsProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
  return api
}

describe('hospital console', () => {
  it('shows an empty state when no case is assigned', async () => {
    renderConsole([])
    expect(await screen.findByText('No cases assigned')).toBeInTheDocument()
    expect(screen.getByText(/Only cases assigned to this hospital/i)).toBeInTheDocument()
  })

  it('lists the pre-alert with reason, vehicle and ETA, and separates reported / confirmed / unknown', async () => {
    renderConsole([pa()])
    const list = await screen.findByRole('complementary', { name: 'Incoming pre-alerts' })
    expect(within(list).getByText('INC-1001')).toBeInTheDocument()
    expect(within(list).getByText('AMB-07')).toBeInTheDocument()
    expect(within(list).getByText('6-8 min')).toBeInTheDocument()
    const detail = screen.getByRole('region', { name: 'Case detail' })
    expect(within(detail).getAllByText('Cardiac (ai extracted)').length).toBeGreaterThan(0)
    expect(within(detail).getAllByText('ai extracted').length).toBeGreaterThan(0)
    expect(within(detail).getAllByText('human confirmed').length).toBeGreaterThan(0)
    expect(within(detail).getAllByText(/Number of patients not known/).length).toBeGreaterThan(0) // unknowns + checklist
  })

  it('handover buttons follow the order: later steps are visibly disabled until earlier ones are done', async () => {
    renderConsole([pa()])
    await screen.findByRole('button', { name: /Acknowledge/ })
    expect(screen.getByRole('button', { name: /Acknowledge/ })).toBeEnabled()
    expect(screen.getByRole('button', { name: /^Arrival/ })).toBeEnabled()
    expect(screen.getByRole('button', { name: /Handover started/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Handover complete/ })).toBeDisabled()
  })

  it('Acknowledge posts to the server', async () => {
    const api = renderConsole([pa()])
    await userEvent.click(await screen.findByRole('button', { name: /Acknowledge/ }))
    await waitFor(() => expect(api.count('POST', '/api/prealerts/PA-0001/acknowledge')).toBe(1))
  })

  it('after arrival the handover can be started, then completed (changes state each time)', async () => {
    let status = 'arrived'
    const api = renderConsole([], {
      'GET /api/prealerts': () => [pa({ status, acknowledged_at: now, arrived_at: now, arrival_source: 'driver', ...(status === 'handover_started' ? { handover_started_at: now } : {}) })],
      'POST /api/prealerts/PA-0001/handover_started': () => { status = 'handover_started'; return {} },
      'POST /api/prealerts/PA-0001/handover_completed': () => { status = 'handover_completed'; return {} },
    })
    const start = await screen.findByRole('button', { name: /Handover started/ })
    expect(start).toBeEnabled()
    await userEvent.click(start)
    await waitFor(() => expect(api.count('POST', '/api/prealerts/PA-0001/handover_started')).toBe(1))
    const done = await screen.findByRole('button', { name: /Handover complete/ }, { timeout: 4000 })
    await waitFor(() => expect(done).toBeEnabled(), { timeout: 4000 })
    await userEvent.click(done)
    await waitFor(() => expect(api.count('POST', '/api/prealerts/PA-0001/handover_completed')).toBe(1))
  })

  it('a redirected (superseded) case says so and offers no actions', async () => {
    renderConsole([pa({ status: 'superseded', superseded_reason: 'destination changed to HOSP-05' })])
    expect(await screen.findByText(/This patient has been redirected: destination changed to HOSP-05/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Acknowledge/ })).toBeNull()
  })

  it('checklist items can be marked provided', async () => {
    const api = renderConsole([pa()])
    const row = (await screen.findByText('Age (not collected in this prototype)')).closest('li')!
    await userEvent.click(within(row).getByRole('button', { name: 'Provided' }))
    await waitFor(() => expect(api.find('POST', '/api/prealerts/PA-0001/checklist')[0]?.body).toMatchObject({ key: 'age', status: 'provided' }))
  })

  it('resources: stale "available" is shown as unknown and can be confirmed, denied or marked unknown', async () => {
    const api = renderConsole([])
    const cath = (await screen.findByText('Cath lab')).closest('li')!
    expect(within(cath).getByText('stale')).toBeInTheDocument()
    expect(within(cath).getByText('unknown')).toBeInTheDocument()
    expect(within(cath).getByText(/reported available, shown unknown/)).toBeInTheDocument()
    await userEvent.click(within(cath).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.find('PATCH', '/api/hospitals/HOSP-02/resources')[0]?.body).toMatchObject({ resource: 'cath_lab', status: 'available', source: 'human_confirmed' }))
    await userEvent.click(within(cath).getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(api.find('PATCH', '/api/hospitals/HOSP-02/resources')[1]?.body).toMatchObject({ status: 'unavailable' }))
    await userEvent.click(within(cath).getByRole('button', { name: 'Unknown' }))
    await waitFor(() => expect(api.find('PATCH', '/api/hospitals/HOSP-02/resources')[2]?.body).toMatchObject({ status: 'unknown' }))
  })
})
