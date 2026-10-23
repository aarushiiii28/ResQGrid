import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import ReplayLab from './ReplayLab'

vi.mock('../../components/MapView', () => ({ default: ({ label }: { label?: string }) => <div data-testid="map" aria-label={label} /> }))

const metrics = (resp: number, p90: number, extra = {}) => ({ incidents: 12, response_median_s: resp, response_p90_s: p90, p1_response_median_s: resp, dest_median_s: resp + 600, dest_p90_s: p90 + 600,
  pct_meeting_requirements: 90, unserved: 0, wasted_dispatches: 3, unnecessary_reassignments: 0, oscillations: 0, preemptions: 0, late_acks: 2, missed_acks: 2,
  dispatch_to_accept_median_s: 24, arrival_to_handover_median_s: 730, coverage_mean: 0.4, eta_calibration: 0.6, diversions: 1, detection_rate: 0.5, detect_median_s: 60, decide_median_s: 75, ...extra })
const result = (p: string, resp: number) => ({
  policy: p, policy_version: `${p}-v1`, description: `${p} desc`, metrics: metrics(resp, resp * 2), end_s: 3600,
  incidents: [], failures: p === 'P0' ? [{ kind: 'no_ack', assignment: 'A001', incident: 'I02', onset_s: 100, detected_s: null, decided_s: null }]
    : [{ kind: 'no_ack', assignment: 'A001', incident: 'I02', onset_s: 100, detected_s: 60, decided_s: 75 }],
  timeline: [{ t: 30, kind: 'call', incident: 'I01', detail: 'cardiac P1' }, { t: 60, kind: 'dispatch', incident: 'I01', detail: 'U01 -> H01', vehicle: 'U01' }],
  tracks: { U01: [[30.9, 75.85], [30.91, 75.86]] },
})
const run = {
  id: 'SIM-0001', label: 'SIMULATED: x', seed: 42, faults: ['route_closure'], config: {}, policy_versions: { P0: 'P0-v1', P1: 'P1-v1', P2: 'P2-v1' },
  world: { hospitals: [{ id: 'H01', lat: 30.9, lon: 75.85, caps: ['emergency'] }], vehicles: [{ id: 'U01', lat: 30.9, lon: 75.85, tags: ['ALS'] }],
           incidents: [{ id: 'I01', t_call: 30, category: 'cardiac', priority: 1, lat: 30.91, lon: 75.86, duplicate_of: null, burst: false }], closures: [] },
  results: { P0: result('P0', 1200), P1: result('P1', 1100), P2: result('P2', 900) },
}
const dist = (vals: number[]) => ({ n: vals.length, mean: 1, median: vals[1], p10: vals[0], p90: vals[2], min: vals[0], max: vals[2], values: vals })
const bench = {
  label: 'SIMULATED: x', seeds: [1, 2, 3], faults: [], policies: ['P0', 'P1', 'P2'], labels: { response_median_s: 'Response time, median (s)', response_p90_s: 'Response time, p90 (s)' },
  distributions: Object.fromEntries(['P0', 'P1', 'P2'].map((p, i) => [p, Object.fromEntries(['response_median_s', 'response_p90_s'].concat(['p1_response_median_s', 'dest_median_s', 'dest_p90_s', 'pct_meeting_requirements', 'unserved', 'wasted_dispatches', 'unnecessary_reassignments', 'oscillations', 'preemptions', 'late_acks', 'missed_acks', 'dispatch_to_accept_median_s', 'arrival_to_handover_median_s', 'coverage_mean', 'eta_calibration', 'diversions', 'detection_rate', 'detect_median_s', 'decide_median_s']).map((k) => [k, dist([1000 - i * 100, 1100 - i * 100, 1200 - i * 100])]))])),
  paired: {},
  where_p2_is_worse_than_p0: [{ metric: 'arrival_to_handover_median_s', label: 'Arrival to handover, median (s)', seeds_worse: 15, of: 20, mean_delta: 20.8, lower_is_better: true }],
}

function renderLab(extra: Record<string, unknown> = {}) {
  signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
  installWebSocket()
  const api = mockApi({ 'POST /api/simulation/runs': run, 'POST /api/simulation/benchmark': bench, ...extra })
  render(<MemoryRouter><AuthProvider><EventsProvider><ReplayLab /></EventsProvider></AuthProvider></MemoryRouter>)
  return api
}

describe('replay lab', () => {
  it('labels everything simulated and never claims lives saved', () => {
    renderLab()
    expect(screen.getByRole('note')).toHaveTextContent(/simulated/i)
    expect(screen.getByRole('note')).toHaveTextContent(/no claim about lives saved/i)
    expect(document.body.textContent).not.toMatch(/\d+ lives saved|saved \d+ lives|lives were saved/i)
  })

  it('seed and fault toggles are sent with the run request', async () => {
    const api = renderLab()
    const seed = screen.getByLabelText('Seed')
    await userEvent.clear(seed)
    await userEvent.type(seed, '7')
    await userEvent.click(screen.getByLabelText(/Stale GPS/))
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    await waitFor(() => expect(api.count('POST', '/api/simulation/runs')).toBe(1))
    const body = api.find('POST', '/api/simulation/runs')[0].body as { seed: number; faults: string[] }
    expect(body.seed).toBe(7)
    expect(body.faults).not.toContain('stale_gps')
    expect(body.faults).toContain('route_closure')
    expect(body.faults).toHaveLength(7)
  })

  it('shows side-by-side maps for P0 and P2, a scrubber, metrics with the best marked, and policy versions', async () => {
    renderLab()
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    expect(await screen.findByText(/SIM-0001 · seed 42/)).toBeInTheDocument()
    expect(screen.getAllByTestId('map')).toHaveLength(2)
    expect(screen.getByLabelText('Left policy')).toHaveValue('P0')
    expect(screen.getByLabelText('Right policy')).toHaveValue('P2')
    expect(screen.getByLabelText('Timeline scrubber')).toBeInTheDocument()
    expect(screen.getByText('P2-v1')).toBeInTheDocument()
    const row = screen.getByText('Response time, median').closest('tr')!
    const cells = within(row).getAllByRole('cell')
    expect(cells[3].className).toMatch(/border-green/) // P2 is fastest here
    expect(cells[1].className).not.toMatch(/border-green/)
  })

  it('scrubbing the timeline changes what the event logs show', async () => {
    renderLab()
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    const log = (await screen.findAllByRole('list', { name: /event log/ }))[0]
    expect(within(log).getByText('Nothing yet.')).toBeInTheDocument()
    const scrub = screen.getByLabelText('Timeline scrubber') as HTMLInputElement
    scrub.focus()
    await userEvent.type(scrub, '{ArrowRight}')
    const { fireEvent } = await import('@testing-library/react')
    fireEvent.change(scrub, { target: { value: '90' } })
    expect(within((await screen.findAllByRole('list', { name: /event log/ }))[0]).getByText(/cardiac P1/)).toBeInTheDocument()
  })

  it('failures table shows P0 missing a failure that P2 detected in 60 s', async () => {
    renderLab()
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    expect(await screen.findByText('not detected')).toBeInTheDocument()
    expect(screen.getAllByText('60 s').length).toBeGreaterThan(0)
  })

  it('exports download JSON and CSV for the saved run', async () => {
    const api = renderLab({ 'GET /api/simulation/runs/SIM-0001/export': 'x' })
    ;(URL as unknown as { createObjectURL: () => string }).createObjectURL = () => 'blob:x'
    ;(URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => {}
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    await userEvent.click(await screen.findByRole('button', { name: 'CSV metrics' }))
    await userEvent.click(screen.getByRole('button', { name: 'JSON' }))
    await waitFor(() => expect(api.count('GET', '/api/simulation/runs/SIM-0001/export')).toBe(2))
  })

  it('benchmark shows distributions across seeds and where P2 is worse', async () => {
    const api = renderLab()
    await userEvent.click(screen.getByRole('button', { name: 'Run benchmark' }))
    await waitFor(() => expect(api.find('POST', '/api/simulation/benchmark')[0]?.body).toMatchObject({ seeds: 10 }))
    expect(await screen.findByText(/Benchmark · 3 seeds/)).toBeInTheDocument()
    expect(screen.getByText('Where ResQGrid (P2) is worse than the baseline')).toBeInTheDocument()
    expect(screen.getByText(/on 15 of 20 seeds/)).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /every seed/ }).length).toBeGreaterThan(0)
  })

  it('a validation error from the server is shown', async () => {
    renderLab({ 'POST /api/simulation/runs': () => new Response(JSON.stringify({ detail: 'unknown fault(s): x' }), { status: 422, headers: { 'content-type': 'application/json' } }) })
    await userEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown fault(s): x')
  })
})
