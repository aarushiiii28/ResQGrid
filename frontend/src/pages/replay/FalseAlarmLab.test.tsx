import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import ReplayLab from './ReplayLab'

vi.mock('../../components/MapView', () => ({ default: () => <div data-testid="map" /> }))

const mix = {
  label: 'SIMULATED: synthetic call mix', seed: 42, n: 300, model_version: 'fa-v1-heuristic', config_hash: 'abc', low_max: 0.35, low_hold_s: 45,
  metrics: { calls: 300, genuine: 86, accidental: 214, false_alarms_flagged: 115, false_alarms_flagged_pct: 53.7, missed_genuine_in_low: 6, missed_genuine_in_low_pct: 7, delayed_genuine_initial_low: 2,
    worst_delay_s: 45, accidental_dispatched: 99, time_to_verify_median_s: 37.3, time_to_verify_p90_s: 45, dispatcher_reviews: 126, dispatcher_reviews_per_100: 42 },
  scenarios: [{ scenario: 'silent_medical', genuine: true, n: 23, low: 5, uncertain: 0, high: 18, median_p: 0.93 }, { scenario: 'child_taps', genuine: false, n: 64, low: 38, uncertain: 3, high: 23, median_p: 0.08 }],
  sweep: [{ threshold: 'low_max', value: 0.35, false_alarms_flagged_pct: 53.7, missed_genuine_in_low: 6, missed_genuine_in_low_pct: 7, dispatcher_reviews_per_100: 42, current: true },
          { threshold: 'high_min', value: 0.55, false_alarms_flagged_pct: 53.7, missed_genuine_in_low: 6, missed_genuine_in_low_pct: 7, dispatcher_reviews_per_100: 38, current: false }],
  failures: [{ scenario: 'silent_medical', p_final: 0.09, initial_lane: 'UNCERTAIN', checkin: 'safe', top: [{ feature: 'checkin', contribution: -2.4 }], missing: ['crash_like'] }],
  delayed: [], where_it_fails: ['Genuine emergencies that look like a deliberate test press are the hard case.'],
}

describe('replay lab: false-alarm mix', () => {
  it('runs the mix and reports MISSED genuine emergencies first, plus the sweep and where it fails', async () => {
    signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
    installWebSocket()
    const api = mockApi({ 'POST /api/simulation/false-alarm': mix })
    render(<MemoryRouter><AuthProvider><EventsProvider><ReplayLab /></EventsProvider></AuthProvider></MemoryRouter>)
    await userEvent.click(screen.getByRole('tab', { name: 'False alarms' }))
    await userEvent.click(screen.getByRole('button', { name: 'Run call mix' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('6 of 86')
    expect(alert).toHaveTextContent(/missed genuine in low lane/i)
    expect(api.find('POST', '/api/simulation/false-alarm')[0].body).toEqual({ seed: 42, n: 300 })
    expect(screen.getByText('115 of 214')).toBeInTheDocument()
    expect(screen.getByText('37.3 s')).toBeInTheDocument()
    expect(screen.getByText('42 per 100 calls')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/simulated/i)
    const sweep = screen.getByText('Threshold sweep').closest('section')!
    expect(within(sweep).getByText('current')).toBeInTheDocument()
    expect(screen.getByText(/Genuine emergencies that look like a deliberate test press/)).toBeInTheDocument()
    expect(screen.getByText(/genuine emergencies left in LOW/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/\d+ lives saved|saved \d+ lives|lives were saved/i)
  })
})
