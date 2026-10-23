import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import CheckIn from './CheckIn'

const view = (over: Record<string, unknown>, ci: Record<string, unknown> = {}) => ({
  incident_id: 'INC-1001', status: 'new', prompt: false, message: 'Are you OK? Tap if safe', countdown_s: 10, dispatch_stopped: false,
  checkin: { state: 'safe', prompt_n: 1, volume: 0.5, reason: 'live', next_in_s: null, ...ci }, ...over,
})

function renderCheckIn(v: object, extra: Record<string, unknown> = {}) {
  signIn({ sub: 'caller-1', role: 'caller', name: 'C' })
  installWebSocket()
  const api = mockApi({ 'GET /api/incidents/INC-1001/check-in': v, ...extra })
  render(<MemoryRouter><AuthProvider><EventsProvider><CheckIn incidentId="INC-1001" /></EventsProvider></AuthProvider></MemoryRouter>)
  return api
}

describe('caller check-in: I am safe', () => {
  it('says no ambulance is being sent when the assignment was stopped, and offers a way back', async () => {
    const api = renderCheckIn(view({ dispatch_stopped: true }), { 'POST /api/incidents/INC-1001/check-in': { state: 'help', dispatch: 'continues' } })
    expect(await screen.findByText(/no ambulance is being sent/)).toBeInTheDocument()
    const back = screen.getByRole('button', { name: 'I need help after all' })
    expect(back.className).toMatch(/min-h-\[56px\]/)
    await userEvent.click(back)
    await waitFor(() => expect(api.find('POST', '/api/incidents/INC-1001/check-in')[0]?.body).toEqual({ response: 'help' }))
  })

  it('is honest when help keeps coming because the phone reported something serious', async () => {
    renderCheckIn(view({ dispatch_stopped: false }))
    expect(await screen.findByText(/Help is still on the way because your phone reported something serious/)).toBeInTheDocument()
    expect(screen.queryByText(/no ambulance is being sent/)).toBeNull()
  })

  it('a cancelled request stays cancelled and on record', async () => {
    renderCheckIn(view({ status: 'cancelled_by_caller', dispatch_stopped: true }, { reason: 'cancelled' }))
    expect(await screen.findByText(/stays cancelled and on record/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'I need help after all' })).toBeNull()
  })
})
