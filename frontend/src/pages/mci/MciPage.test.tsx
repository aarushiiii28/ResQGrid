import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import MciPage from './MciPage'

const patient = (i: number, extra = {}) => ({ id: `P-0${i}`, triage: 'unassigned', confirmed: false, suggested: null, suggested_evidence: null, set_by: null, note: null, ...extra })
const plan = {
  patient_hospital: { 'P-01': 'HOSP-01' }, overflow: ['P-03'],
  loads: [{ id: 'LOAD-1', patient_ids: ['P-01'], triage_mix: { red: 1 }, hospital_id: 'HOSP-01', vehicle_needs: ['ALS'], vehicle_id: 'AMB-09', eta_scene_s: 300, eta_hospital_s: 500, status: 'planned' }],
  hospital_loads: [{ hospital_id: 'HOSP-01', name: 'Synthetic General 01', capacity: 2, red_capacity: 1, planned: 2, planned_red: 1, en_route: 1, fraction: 1, unverified_resources: [], note: null },
                   { hospital_id: 'HOSP-05', name: 'Synthetic General 05', capacity: 0, red_capacity: 0, planned: 0, planned_red: 0, en_route: 0, fraction: 0, unverified_resources: ['er_bay'], note: 'capacity data unverified: counted as zero until the hospital confirms' }],
  unconfirmed: ['P-03'], warnings: ['1 patient(s) exceed the capacity of every eligible hospital: request mutual aid or divert (simulated escalation).'],
  rules: ['Black is never transported by the planner'],
  split: { ortools: { cost: 12.5, assigned: 2, overflow: 1, status: 'OPTIMAL' }, greedy: { cost: 14, assigned: 2, overflow: 1 } },
  assignment: { ortools: { cost: 8, assigned: 1, unassigned: [], status: 'OPTIMAL' }, greedy: { cost: 9, assigned: 1, unassigned: [] } },
}
const mci = (extra = {}) => ({ id: 'MCI-001', incident_id: 'INC-1001', patients: [patient(1, { triage: 'red', confirmed: true, set_by: 'disp-1' }), patient(2, { suggested: 'amber', suggested_evidence: '1 amber' }), patient(3)], status: 'triage', approved_by: null, plan: null, ...extra })
const inc = (id: string) => ({ id, status: 'new', reported_category: { value: 'trauma' } })

function renderPage(list: unknown[], extra: Record<string, unknown> = {}) {
  signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
  installWebSocket()
  const api = mockApi({
    'GET /api/incidents': [inc('INC-1001'), inc('INC-1002')],
    'GET /api/mci': () => list,
    'GET /api/hospitals': [{ id: 'HOSP-01', name: 'Synthetic General 01' }],
    'POST /api/mci': mci(),
    'PATCH /api/mci/MCI-001/patients/*': mci(),
    'POST /api/mci/MCI-001/suggest': mci(),
    'POST /api/mci/MCI-001/plan': mci({ status: 'planned', plan }),
    'POST /api/mci/MCI-001/approve': mci({ status: 'approved', approved_by: 'disp-1', plan }),
    ...extra,
  })
  render(<MemoryRouter><AuthProvider><EventsProvider><MciPage /></EventsProvider></AuthProvider></MemoryRouter>)
  return api
}

describe('mass-casualty page', () => {
  it('opens a record for an incident with a patient count', async () => {
    const api = renderPage([])
    await screen.findByText('Open a record')
    await userEvent.click(await screen.findByRole('button', { name: 'Open mass-casualty record' }))
    await waitFor(() => expect(api.find('POST', '/api/mci')[0]?.body).toEqual({ incident_id: 'INC-1001', patients: 8 }))
  })

  it('triage colours are labelled text buttons set by a person; suggestions are shown but marked unconfirmed', async () => {
    const api = renderPage([mci()])
    const row2 = (await screen.findByText('P-02')).closest('tr')!
    expect(within(row2).getByText(/suggested amber/)).toBeInTheDocument()
    expect(within(row2).getByText('unconfirmed')).toBeInTheDocument()
    expect(within(row2).getByText('planned as red')).toBeInTheDocument() // unknown is not minor
    const row1 = screen.getByText('P-01').closest('tr')!
    expect(within(row1).getByRole('button', { name: 'P-01 red' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(within(row2).getByRole('button', { name: 'P-02 amber' }))
    await waitFor(() => expect(api.find('PATCH', '/api/mci/MCI-001/patients/P-02')[0]?.body).toEqual({ triage: 'amber' }))
  })

  it('suggest posts the text; plan does not commit anything', async () => {
    const api = renderPage([mci()])
    await userEvent.type(await screen.findByPlaceholderText(/3 red, 2 amber/), '2 red, 1 black')
    await userEvent.click(screen.getByRole('button', { name: 'Suggest' }))
    await waitFor(() => expect(api.find('POST', '/api/mci/MCI-001/suggest')[0]?.body).toEqual({ text: '2 red, 1 black' }))
    await userEvent.click(screen.getByRole('button', { name: 'Plan split and units' }))
    await waitFor(() => expect(api.count('POST', '/api/mci/MCI-001/plan')).toBe(1))
    expect(api.count('POST', '/api/mci/MCI-001/approve')).toBe(0)
  })

  it('a planned record shows hospital load bars, overflow warning, loads and optimiser against greedy', async () => {
    renderPage([mci({ status: 'planned', plan })])
    expect(await screen.findByRole('alert')).toHaveTextContent(/exceed the capacity of every eligible hospital/)
    const bar = screen.getByRole('img', { name: /2 planned of 2 capacity, 1 en route, 1 red/ })
    expect(bar).toBeInTheDocument()
    expect(screen.getByText(/capacity data unverified: counted as zero/)).toBeInTheDocument()
    const loads = screen.getByText('LOAD-1').closest('tr')!
    expect(within(loads).getByText('AMB-09')).toBeInTheDocument()
    expect(screen.getByText(/Patient split, OR-Tools cost/)).toBeInTheDocument()
    expect(screen.getByText('12.5 (optimal)')).toBeInTheDocument()
  })

  it('approve is disabled until a reason is given, then posts it', async () => {
    const api = renderPage([mci({ status: 'planned', plan })])
    const btn = await screen.findByRole('button', { name: 'Approve plan' })
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Reason for approving/), 'reviewed with the commander')
    await userEvent.click(btn)
    await waitFor(() => expect(api.find('POST', '/api/mci/MCI-001/approve')[0]?.body).toEqual({ reason: 'reviewed with the commander' }))
  })
})
