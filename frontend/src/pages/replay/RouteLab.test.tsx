import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import ReplayLab from './ReplayLab'

vi.mock('../../components/MapView', () => ({ default: () => <div data-testid="map" /> }))

const stat = (mean: number | null) => ({ mean, median: mean, n: mean === null ? 0 : 8 })
const res = {
  label: 'SIMULATED: toy city', seeds: [1, 2, 3], policy_version: 'stable-v1', names: { A: 'Reroute on every change', B: 'Never reroute', C: 'ResQGrid stable policy' },
  metrics: [{ key: 'total_time_s', label: 'Total travel time (s)' }, { key: 'reroutes', label: 'Route changes' }, { key: 'eta_std_s', label: 'ETA variability, std dev (s)' }],
  summary: { A: { total_time_s: stat(718), reroutes: stat(2.2), eta_std_s: stat(126) }, B: { total_time_s: stat(1143), reroutes: stat(0), eta_std_s: stat(374) }, C: { total_time_s: stat(727), reroutes: stat(1.6), eta_std_s: stat(82) } },
  per_seed: [{ seed: 1, has_wrong_turn: true, has_closure: false, congestion_zones: 2, policies: { A: { total_time_s: 700, reroutes: 2 }, B: { total_time_s: 900, reroutes: 0 }, C: { total_time_s: 710, reroutes: 1 } } }],
  where_stable_is_worse: [{ vs: 'A', vs_name: 'Reroute on every change', metric: 'total_time_s', label: 'Total travel time (s)', seeds_worse: 3, of: 14, mean_delta: 91.7 },
                          { vs: 'B', vs_name: 'Never reroute', metric: 'reroutes', label: 'Route changes', seeds_worse: 13, of: 14, mean_delta: 1.8 }],
  sweep: [{ setting: 'min_saving_s', value: 60, current: true, mean_reroutes: 1.17, mean_total_time_s: 666.7, mean_eta_std_s: 113.8 }, { setting: 'sustained_evaluations', value: 4, current: false, mean_reroutes: 1.17, mean_total_time_s: 667.5, mean_eta_std_s: 125.8 }],
  thresholds: { congestion: {}, off_route: {}, rejoin: {} },
}

describe('replay lab: stable routes', () => {
  it('compares the three policies, marks the best per metric, and says where the stable policy is worse', async () => {
    signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
    installWebSocket()
    const api = mockApi({ 'POST /api/simulation/routes': res })
    render(<MemoryRouter><AuthProvider><EventsProvider><ReplayLab /></EventsProvider></AuthProvider></MemoryRouter>)
    await userEvent.click(screen.getByRole('tab', { name: 'Stable routes' }))
    await userEvent.click(screen.getByRole('button', { name: 'Run route comparison' }))
    expect(await screen.findByText(/Route policies · 3 trips/)).toBeInTheDocument()
    expect(api.find('POST', '/api/simulation/routes')[0].body).toEqual({ seed: 1, n: 8, sweep: true })
    const results = screen.getByText('Results').closest('section')!
    const row = within(results).getByText('ETA variability, std dev (s)').closest('tr')!
    expect(within(row).getByText('best').closest('td')).toHaveTextContent('82')                    // the stable policy is the most stable
    const reroutes = within(results).getByText('Route changes').closest('tr')!
    expect(within(reroutes).getByText('best').closest('td')).toHaveTextContent('0')                  // never-reroute changes least
    const worse = screen.getByText('Where the stable policy is worse').closest('section')!
    expect(worse).toHaveTextContent('worse than A')
    expect(worse).toHaveTextContent('on 3 of 14 trips')
    expect(worse).toHaveTextContent('worse than B')
    const sweep = screen.getByText('Threshold sweep').closest('section')!
    expect(within(sweep).getByText('current')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/simulated/i)
    expect(document.body.textContent).not.toMatch(/\d+ lives saved|saved \d+ lives|lives were saved/i)
  })
})
