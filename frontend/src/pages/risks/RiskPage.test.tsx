import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import RiskPage from './RiskPage'

const now = new Date().toISOString()
const eff = (extra = {}) => ({ time_delta_s: 0, suitability: 'unchanged', suitability_note: 'note', coverage_delta: 0, other_incidents: [], uncertainty: 'estimates', ...extra })
const opt = (kind: string, extra = {}) => ({
  id: `RSK-0001-${kind}`, kind, label: kind.replace(/_/g, ' '), description: `${kind} description`, effects: eff(), trade_offs: [], benefit: null,
  changes_plan: false, necessary: false, below_threshold: false, recommended: false, payload: {}, ...extra,
})
const risk = (extra = {}) => ({
  id: 'RSK-0001', incident_id: 'INC-1001', type: 'no_acknowledgment', severity: 'high', entity: 'AMB-07',
  evidence: [{ key: 'waited_s', value: 65, at: now }, { key: 'assignment_state', value: 'sent', at: now }],
  affected_assumption: 'AMB-07 has received and accepted the assignment', detected_at: now, last_seen_at: now, status: 'open', confirmations: [],
  escalated: false, resolution: null, seen_count: 3,
  options: [
    opt('keep_current_plan', { label: 'Keep current plan', effects: eff({ suitability_note: 'the risk stays unresolved' }), trade_offs: ['Assumption stays unverified'] }),
    opt('request_confirmation'),
    opt('propose_eligible_unit', { label: 'Propose unit AMB-09', changes_plan: true, necessary: true, recommended: true, benefit: 6.2,
      effects: eff({ time_delta_s: -90, suitability: 'improved', coverage_delta: -0.012, other_incidents: [{ incident_id: 'INC-1002', impact: 'no effect' }] }), trade_offs: ['AMB-07 stops being used'] }),
    opt('escalate'),
  ],
  ...extra,
})

function renderPage(list: unknown[], extra: Record<string, unknown> = {}) {
  signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
  installWebSocket()
  const api = mockApi({
    'GET /api/risks': () => list,
    'GET /api/dispatch/autopilot': { enabled: true },
    'POST /api/risks/evaluate': [],
    'POST /api/risks/RSK-0001/recover': risk({ status: 'resolved', resolution: { option: 'propose_eligible_unit', by: 'disp-1' } }),
    ...extra,
  })
  render(
    <MemoryRouter>
      <AuthProvider>
        <EventsProvider>
          <RiskPage />
        </EventsProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
  return api
}

describe('risk and recovery page', () => {
  it('empty state explains how to see a risk', async () => {
    renderPage([])
    expect(await screen.findByText('No open risks')).toBeInTheDocument()
  })

  it('shows evidence, the affected assumption, severity bar, and deduplicated sightings', async () => {
    renderPage([risk()])
    const detail = await screen.findByRole('region', { name: 'Risk detail' })
    expect(await within(detail).findByText(/AMB-07 has received and accepted the assignment/)).toBeInTheDocument()
    expect(await within(detail).findByText('waited s')).toBeInTheDocument()
    expect(await within(detail).findByText('65')).toBeInTheDocument()
    const list = screen.getByRole('complementary', { name: 'Risks' })
    expect(within(list).getByText(/seen 3x/)).toBeInTheDocument()
    expect(within(list).getByText('high', { selector: '.sr-only' })).toBeInTheDocument() // severity is text, not colour alone
  })

  it('keep current plan is always listed, with effects on time, suitability, coverage and other incidents', async () => {
    renderPage([risk()])
    await screen.findByText('Keep current plan')
    expect(screen.getByRole('radio', { name: 'Keep current plan' })).toBeInTheDocument()
    const unit = screen.getByRole('radio', { name: 'Propose unit AMB-09' }).closest('li')!
    expect(within(unit).getByText('-1.5 min')).toBeInTheDocument()
    expect(within(unit).getByText('improved')).toBeInTheDocument()
    expect(within(unit).getByText('-1.2 pts')).toBeInTheDocument()
    expect(within(unit).getByText(/INC-1002/)).toBeInTheDocument()
    expect(within(unit).getByText('recommended')).toBeInTheDocument()
    expect(within(unit).getByText('current plan unsafe')).toBeInTheDocument()
    expect(within(unit).getByText('changes the plan')).toBeInTheDocument()
  })

  it('a plan change needs a reason: Approve is disabled until one is typed, then it posts the chosen option', async () => {
    const api = renderPage([risk()])
    const approve = await screen.findByRole('button', { name: /Approve:/ })
    expect(approve).toBeDisabled()
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Propose unit AMB-09' })).toBeChecked()) // recommended is preselected, not applied
    await userEvent.type(screen.getByLabelText('Reason (required)'), 'crew silent')
    expect(approve).toBeEnabled()
    await userEvent.click(approve)
    await waitFor(() => expect(api.find('POST', '/api/risks/RSK-0001/recover')[0]?.body).toEqual({ option_kind: 'propose_eligible_unit', reason: 'crew silent' }))
  })

  it('choosing keep current plan posts that instead', async () => {
    const api = renderPage([risk()])
    await userEvent.click(await screen.findByRole('radio', { name: 'Keep current plan' }))
    await userEvent.type(screen.getByLabelText('Reason (required)'), 'crew radioed')
    await userEvent.click(screen.getByRole('button', { name: /Approve: keep current plan/ }))
    await waitFor(() => expect(api.find('POST', '/api/risks/RSK-0001/recover')[0]?.body).toMatchObject({ option_kind: 'keep_current_plan' }))
  })

  it('Evaluate now calls the monitor and says nothing new was raised (dedup)', async () => {
    const api = renderPage([risk()])
    await userEvent.click(await screen.findByRole('button', { name: 'Evaluate now' }))
    await waitFor(() => expect(api.count('POST', '/api/risks/evaluate')).toBe(1))
    expect(await screen.findByText(/No new risks/)).toBeInTheDocument()
  })

  it('resolved risks are hidden until requested and show who decided what', async () => {
    renderPage([risk({ status: 'resolved', resolution: { option: 'keep_current_plan', by: 'disp-1', reason: 'crew radioed' } })])
    expect(await screen.findByText('No open risks')).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText(/show resolved/i))
    expect(await screen.findByText(/keep current plan by disp-1: crew radioed/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Approve:/ })).toBeNull()
  })

  it('a server error is shown with a retry, not swallowed', async () => {
    renderPage([], { 'GET /api/risks': () => new Response('x', { status: 500 }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 500')
  })
})
