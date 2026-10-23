import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { setTimingOverride } from '../../lib/alerts'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { geolocation, holdSos, installWebSocket, mockApi, signIn } from '../../test/helpers'
import CallerPage from './CallerPage'

vi.mock('../../components/MapView', () => ({ default: ({ label }: { label?: string }) => <div data-testid="map" aria-label={label} /> }))

const OPENING = 'Emergency line. Your location is shared and help is being arranged. What happened?'
const tracker = {
  incident_id: 'INC-1001', status: 'new', headline: 'Finding the nearest ambulance.', stages: [], current_stage: null, category: 'unknown',
  needs_review: false, location: { lat: 30.9, lon: 75.85, accuracy_m: 14, approximate: false, source: 'gps' }, vehicle: null, route: null,
  eta: null, hospital: null, assignment: null, pipeline: [], autopilot: { enabled: true, label: 'Demo Autopilot' },
}

function routes(extra: Record<string, unknown> = {}) {
  return mockApi({
    'POST /api/sos/start': { incident_id: 'INC-1001', status: 'pending' },
    'POST /api/incidents/INC-1001/sos/confirm': { incident_id: 'INC-1001', call_id: 'CALL-0001', operator: OPENING },
    'POST /api/incidents/INC-1001/sos/cancel': { incident_id: 'INC-1001', status: 'cancelled_by_caller' },
    'GET /api/tracker/INC-1001': tracker,
    'GET /api/incidents/INC-1001/aed': [],
    'POST /api/incidents/INC-1001/location': {},
    'POST /api/incidents/INC-1001/signals': { accepted: 1 },
    ...extra,
  })
}

function renderCaller() {
  signIn({ sub: 'caller-1', role: 'caller', name: 'Caller 1' })
  installWebSocket()
  return render(<MemoryRouter><AuthProvider><EventsProvider><CallerPage /></EventsProvider></AuthProvider></MemoryRouter>)
}

describe('layer 1: accidental-press prevention', () => {
  it('a quick tap does nothing: SOS needs a press and hold, and the label says so', async () => {
    const api = routes()
    geolocation('ok')
    renderCaller()
    const btn = screen.getByRole('button', { name: /SOS/ })
    expect(btn).toHaveAccessibleName(/hold/i)
    await userEvent.click(btn)
    await new Promise((r) => setTimeout(r, 200))
    expect(api.count('POST', '/api/sos/start')).toBe(0)
    expect(screen.queryByTestId('countdown-number')).toBeNull()
  })

  it('releasing before the hold is complete cancels it', async () => {
    setTimingOverride({ holdMs: 400, countdownS: 0.3 })
    const api = routes()
    geolocation('ok')
    renderCaller()
    const user = userEvent.setup()
    await user.pointer({ keys: '[MouseLeft>]', target: screen.getByRole('button', { name: /SOS/ }) })
    await new Promise((r) => setTimeout(r, 120))
    await user.pointer({ keys: '[/MouseLeft]' })
    await new Promise((r) => setTimeout(r, 450))
    expect(api.count('POST', '/api/sos/start')).toBe(0)
    expect(screen.getByTestId('hold-ring')).toHaveAttribute('stroke-dashoffset', expect.any(String))
  })

  it('shows a progress ring while holding', async () => {
    setTimingOverride({ holdMs: 600, countdownS: 0.3 })
    routes()
    geolocation('ok')
    renderCaller()
    const user = userEvent.setup()
    await user.pointer({ keys: '[MouseLeft>]', target: screen.getByRole('button', { name: /SOS/ }) })
    await waitFor(() => expect(screen.getByRole('progressbar', { name: 'SOS hold progress' })).toHaveAttribute('aria-valuenow', expect.stringMatching(/^[1-9]/)))
    await user.pointer({ keys: '[/MouseLeft]' })
  })

  it('after the hold a PENDING request starts the countdown with a huge Cancel, and the call opens only when it ends', async () => {
    setTimingOverride({ holdMs: 60, countdownS: 1.2 })
    const api = routes()
    geolocation('ok')
    renderCaller()
    await holdSos()
    expect(await screen.findByTestId('countdown-number')).toBeInTheDocument()
    expect(api.count('POST', '/api/sos/start')).toBe(1)
    expect(api.count('POST', '/api/incidents/INC-1001/sos/confirm')).toBe(0)     // not live yet
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    expect(cancel.className).toMatch(/min-h-\[120px\]/)
    expect(screen.queryByText(OPENING)).toBeNull()
    expect(await screen.findByText(OPENING, {}, { timeout: 3000 })).toBeInTheDocument()   // went live
    expect(api.count('POST', '/api/incidents/INC-1001/sos/confirm')).toBe(1)
  })

  it('Cancel keeps the record, never opens the call, and offers a check-in', async () => {
    setTimingOverride({ holdMs: 60, countdownS: 5 })
    const api = routes({ 'GET /api/incidents/INC-1001/check-in': {
      incident_id: 'INC-1001', status: 'cancelled_by_caller', prompt: true, message: 'Are you OK? Tap if safe', countdown_s: 10,
      checkin: { state: 'prompting', prompt_n: 1, volume: 0.5, reason: 'cancelled', next_in_s: 14 },
    }, 'POST /api/incidents/INC-1001/check-in': { state: 'safe', lane: 'LOW' } })
    geolocation('ok')
    renderCaller()
    await holdSos()
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByText('SOS cancelled')).toBeInTheDocument()
    expect(screen.getByText(/The record is kept/)).toBeInTheDocument()
    expect(api.count('POST', '/api/incidents/INC-1001/sos/cancel')).toBe(1)
    expect(api.count('POST', '/api/incidents/INC-1001/sos/confirm')).toBe(0)
    expect(screen.queryByText(OPENING)).toBeNull()
    const safe = await screen.findByRole('button', { name: 'I am safe' })
    expect(safe.className).toMatch(/min-h-\[88px\]/)
    await userEvent.click(safe)
    await waitFor(() => expect(api.find('POST', '/api/incidents/INC-1001/check-in')[0]?.body).toEqual({ response: 'safe' }))
  })

  it('the countdown shows large numerals that count down', async () => {
    setTimingOverride({ holdMs: 60, countdownS: 3 })
    routes()
    geolocation('ok')
    renderCaller()
    await holdSos()
    const n = await screen.findByTestId('countdown-number')
    expect(Number(n.textContent)).toBeLessThanOrEqual(3)
    expect(n.className).toMatch(/font-display/)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  })

  it('mic analysis is off until the caller taps Allow, and says nothing is recorded', async () => {
    setTimingOverride({ holdMs: 60, countdownS: 0.2 })
    routes()
    geolocation('ok')
    renderCaller()
    await holdSos()
    await screen.findByText(OPENING, {}, { timeout: 3000 })
    expect(screen.getByText(/Nothing is recorded or sent/)).toBeInTheDocument()
    expect(screen.getByText('Off.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Allow' }).className).toMatch(/min-h-\[56px\]/)
  })

  it('shows the I-am-safe check-in with 88px buttons when the server prompts', async () => {
    setTimingOverride({ holdMs: 60, countdownS: 0.2 })
    routes({ 'GET /api/incidents/INC-1001/check-in': {
      incident_id: 'INC-1001', status: 'new', prompt: true, message: 'Are you OK? Tap if safe', countdown_s: 10,
      checkin: { state: 'prompting', prompt_n: 2, volume: 0.75, reason: 'live', next_in_s: 5 },
    } })
    geolocation('ok')
    renderCaller()
    await holdSos()
    expect(await screen.findByRole('button', { name: 'I need help' }, { timeout: 3000 })).toBeInTheDocument()
    expect(screen.getByText('Reminder 2 of 3')).toBeInTheDocument()
  })
})
