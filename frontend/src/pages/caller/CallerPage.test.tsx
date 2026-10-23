import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { ev, FakeWebSocket, geolocation, holdSos, installWebSocket, mockApi, signIn } from '../../test/helpers'
import CallerPage from './CallerPage'

vi.mock('../../components/MapView', () => ({
  default: ({ label }: { label?: string }) => <div data-testid="map" aria-label={label} />,
}))

const tracker = (over: Record<string, unknown> = {}) => ({
  incident_id: 'INC-1001',
  status: 'dispatched',
  headline: 'Finding the nearest ambulance.',
  stages: [
    { key: 'call_started', label: 'Call started', state: 'done', at: new Date().toISOString() },
    { key: 'unit_sent', label: 'Ambulance sent', state: 'active', at: null },
  ],
  current_stage: 'unit_sent',
  category: 'unknown',
  needs_review: false,
  location: { lat: 30.9, lon: 75.85, accuracy_m: 14, approximate: false, source: 'gps' },
  vehicle: null,
  route: null,
  eta: null,
  hospital: null,
  assignment: null,
  pipeline: [],
  autopilot: { enabled: true, label: 'Demo Autopilot' },
  ...over,
})

const OPENING = 'Emergency line. Your location is shared and help is being arranged. What happened?'

function routes(extra: Record<string, unknown> = {}) {
  return mockApi({
    'POST /api/sos/start': { incident_id: 'INC-1001', status: 'pending' },
    'POST /api/incidents/INC-1001/sos/confirm': { incident_id: 'INC-1001', call_id: 'CALL-0001', operator: OPENING },
    'GET /api/tracker/INC-1001': () => tracker(),
    'GET /api/incidents/INC-1001/aed': [],
    'POST /api/incidents/INC-1001/location': { ok: true },
    'GET /api/first-aid': { label: 'General guidance, follow the call-taker.', note: 'n', categories: [] },
    ...extra,
  })
}

function renderCaller() {
  signIn({ sub: 'caller-1', role: 'caller', name: 'Caller 1' })
  installWebSocket()
  return render(
    <MemoryRouter>
      <AuthProvider>
        <EventsProvider>
          <CallerPage />
        </EventsProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('caller: one press', () => {
  it('shows the SOS control, the 112 notice, and no form fields before the press', () => {
    routes()
    geolocation('ok')
    renderCaller()
    expect(screen.getByRole('button', { name: /SOS/ })).toBeInTheDocument()
    expect(screen.getByText(/In a real emergency dial 112/)).toBeInTheDocument()
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(document.querySelector('a[href^="tel:"], a[href^="sms:"]')).toBeNull()
  })

  it('SOS creates the incident with location, accuracy and age, and opens the call, with no further input', async () => {
    const api = routes()
    const watch = geolocation('ok', { latitude: 30.9012, longitude: 75.8581, accuracy: 9 })
    renderCaller()
    await holdSos()
    expect(watch).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(api.count('POST', '/api/sos/start')).toBe(1))
    const body = api.find('POST', '/api/sos/start')[0].body as Record<string, unknown>
    expect(body).toMatchObject({ lat: 30.9012, lon: 75.8581, accuracy_m: 9, approximate: false, location_source: 'gps' })
    expect(typeof body.location_age_s).toBe('number')
    expect(await screen.findByText(OPENING)).toBeInTheDocument()
    expect(screen.getByText('Simulated')).toBeInTheDocument()
    expect(screen.getByLabelText('Call timer')).toHaveTextContent(/^\d\d:\d\d$/)
    expect(await screen.findByTestId('headline')).toHaveTextContent('Finding the nearest ambulance.')
    expect(api.count('POST', '/api/sos/start')).toBe(1) // exactly one incident per press
  })

  it('GPS denied: still proceeds with an approximate pin and tells the caller', async () => {
    const api = routes({
      'GET /api/tracker/INC-1001': () => tracker({ location: { lat: 30.901, lon: 75.8573, accuracy_m: null, approximate: true, source: 'default' } }),
    })
    geolocation('denied')
    renderCaller()
    await holdSos()
    await waitFor(() => expect(api.count('POST', '/api/sos/start')).toBe(1))
    const body = api.find('POST', '/api/sos/start')[0].body as Record<string, unknown>
    expect(body).toMatchObject({ approximate: true, location_source: 'default' })
    expect(await screen.findByText(/Approximate location\. Drag the pin/)).toBeInTheDocument()
    expect(screen.getByTestId('map')).toBeInTheDocument()
  })

  it('no GPS fix within 2.5 s: creates the incident anyway (does not make the caller wait)', async () => {
    const api = routes()
    Object.defineProperty(globalThis.navigator, 'geolocation', { configurable: true, value: { watchPosition: vi.fn(() => 1), clearWatch: vi.fn() } })
    renderCaller()
    await holdSos()
    expect(api.count('POST', '/api/sos/start')).toBe(0)
    await waitFor(() => expect(api.count('POST', '/api/sos/start')).toBe(1), { timeout: 4000 })
    expect(api.find('POST', '/api/sos/start')[0].body).toMatchObject({ approximate: true })
  })

  it('shows a retry when the server is unreachable instead of failing silently', async () => {
    mockApi({ 'POST /api/sos/start': () => new Response(JSON.stringify({ detail: 'boom' }), { status: 500, headers: { 'content-type': 'application/json' } }) })
    geolocation('ok')
    renderCaller()
    await holdSos()
    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})

describe('caller: call screen', () => {
  async function openCall(api = routes()) {
    geolocation('ok')
    renderCaller()
    await holdSos()
    await screen.findByText(OPENING)
    return api
  }

  it('typed fallback works without a microphone, and the operator reply appears', async () => {
    const api = await openCall(
      routes({
        'POST /api/calls/CALL-0001/utterances': (b: unknown) => ({
          transcript: [
            { speaker: 'operator', text: OPENING, at: new Date().toISOString(), mode: 'scripted' },
            { speaker: 'caller', text: (b as { text: string }).text, at: new Date().toISOString(), mode: 'typed' },
            { speaker: 'operator', text: 'Is the person awake and breathing?', at: new Date().toISOString(), mode: 'scripted' },
          ],
          operator: 'Is the person awake and breathing?',
          questions_asked: 2,
          finished: false,
        }),
      }),
    )
    expect(screen.getByRole('button', { name: /Voice not available/ })).toBeDisabled() // jsdom has no SpeechRecognition
    await userEvent.type(screen.getByLabelText('Type your answer'), 'chest pain{enter}')
    await waitFor(() => expect(api.count('POST', '/api/calls/CALL-0001/utterances')).toBe(1))
    expect(api.find('POST', '/api/calls/CALL-0001/utterances')[0].body).toEqual({ text: 'chest pain', mode: 'typed' })
    expect((await screen.findAllByText('Is the person awake and breathing?')).length).toBeGreaterThan(0)
  })

  it('quick-reply buttons are at least 56px tall and send mode "quick"', async () => {
    const api = await openCall(
      routes({
        'POST /api/calls/CALL-0001/utterances': { transcript: [], operator: 'How many people need help?', questions_asked: 2, finished: false },
      }),
    )
    const btn = screen.getByRole('button', { name: 'Chest pain' })
    expect(btn.className).toMatch(/min-h-\[56px\]/)
    await userEvent.click(btn)
    await waitFor(() => expect(api.find('POST', '/api/calls/CALL-0001/utterances')[0]?.body).toEqual({ text: 'Chest pain', mode: 'quick' }))
  })

  it('mute and end call change state; ending the call posts to the server', async () => {
    const api = await openCall(routes({ 'POST /api/calls/CALL-0001/end': { ended_at: 'now' } }))
    const mute = screen.getByRole('button', { name: 'Mute' })
    await userEvent.click(mute)
    expect(screen.getByRole('button', { name: 'Unmute' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(screen.getByRole('button', { name: 'End call' }))
    await waitFor(() => expect(api.count('POST', '/api/calls/CALL-0001/end')).toBe(1))
    expect(await screen.findByText('Call ended')).toBeInTheDocument()
  })

  it('every call control is 56px or larger', async () => {
    await openCall()
    for (const name of ['Mute', 'End call']) expect(screen.getByRole('button', { name }).className).toMatch(/min-h-\[56px\]/)
    expect(screen.getByLabelText('Type your answer').className).toMatch(/min-h-\[56px\]/)
  })
})

describe('caller: live status tracker', () => {
  it('updates when events arrive over the WebSocket, with plain text', async () => {
    let current = tracker()
    routes({ 'GET /api/tracker/INC-1001': () => current })
    geolocation('ok')
    renderCaller()
    await holdSos()
    const headline = await screen.findByTestId('headline')
    expect(headline).toHaveTextContent('Finding the nearest ambulance.')

    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0))
    current = tracker({
      headline: 'Ambulance accepted. ETA 6-8 min.',
      eta: { low_s: 360, high_s: 480, text: '6-8 min', phase: 'scene' },
      vehicle: { id: 'AMB-07', lat: 30.9, lon: 75.85, location_age_s: 3, source: 'simulated', status: 'accepted', state: 'live', simulated: true },
      hospital: { id: 'HOSP-02', name: 'Synthetic General 02', lat: 30.91, lon: 75.86 },
      assignment: { id: 'ASG-0001', state: 'accepted', late: false },
    })
    FakeWebSocket.instances[0].emit(ev('assignment.accepted', { seq: 5 }))
    await waitFor(() => expect(screen.getByTestId('headline')).toHaveTextContent('Ambulance accepted. ETA 6-8 min.'), { timeout: 3000 })
    expect(screen.getByText('AMB-07')).toBeInTheDocument()
    expect(screen.getByText('Synthetic General 02')).toBeInTheDocument()
    expect(screen.getByText(/Ambulance movement simulated/)).toBeInTheDocument()
  })

  it('says so when the realtime link drops and keeps showing the last update', async () => {
    routes()
    geolocation('ok')
    renderCaller()
    await holdSos()
    await screen.findByTestId('headline')
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(0))
    FakeWebSocket.instances[0].drop()
    expect(await screen.findByText(/Reconnecting/)).toBeInTheDocument()
    expect(screen.getByTestId('headline')).toHaveTextContent('Finding the nearest ambulance.')
  })

  it('first-aid and AED panels are reachable and labelled', async () => {
    routes({
      'GET /api/first-aid': {
        label: 'General guidance, follow the call-taker.',
        note: 'Static guidance.',
        categories: [{ key: 'cpr', title: 'Chest compressions', applies_to: ['cardiac'], rhythm_bpm: 110, steps: [{ text: 'Push hard and fast.', say: 'Push hard and fast.' }] }],
      },
      'GET /api/incidents/INC-1001/volunteer-alerts': [],
    })
    geolocation('ok')
    renderCaller()
    await holdSos()
    await screen.findByTestId('headline')
    await userEvent.click(screen.getByRole('tab', { name: 'First aid' }))
    expect(await screen.findByText('General guidance, follow the call-taker.')).toBeInTheDocument()
    expect(screen.getByText('110 per minute')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'AED + help' }))
    const panel = await screen.findByLabelText('Nearby AED and volunteers')
    expect(within(panel).getByText(/Synthetic, opt-in volunteers/)).toBeInTheDocument()
  })
})
