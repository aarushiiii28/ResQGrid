import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { FaView, Incident } from '../../lib/types'
import { mockApi } from '../../test/helpers'
import FalseAlarmPanel, { LaneStamps } from './FalseAlarmPanel'

const inc = (over: Partial<Incident> = {}) => ({ id: 'INC-1001', status: 'new', created_at: new Date(Date.now() - 65_000).toISOString(), ...over }) as Incident
const c = (feature: string, evidence: number, weight: number, extra = {}) => ({ feature, evidence, weight, contribution: Math.round(evidence * weight * 100) / 100, status: 'available', note: null, ...extra })
const fa = (over: Partial<FaView> = {}): FaView => ({
  incident_id: 'INC-1001', status: 'new', created_at: new Date().toISOString(), live_at: null, lane: 'LOW', probability_genuine: 0.12, base_lane: 'LOW',
  model_version: 'fa-v1-heuristic', config_hash: 'abcd1234abcd1234', bias: 0.5, logit: -2.34,
  contributions: [c('pocket_motion', 1, 1.4, { contribution: -1.4, evidence: -1 }), c('touch_random', -0.9, 1.6, { contribution: -1.44 }), c('crash_like', 0, 3, { status: 'unavailable', note: 'sensor unavailable' })],
  top: [], missing: ['crash_like'], overrides: [], place_type: 'residential', corroborating: [], mic_consent: false,
  checkin: { state: 'prompting', reason: 'live', prompt_n: 2, volume: 0.75, last_prompt_at: null, next_in_s: 8, response: null },
  hold_remaining_s: 31, dispatch_started: false, awaiting_review: false, followup_alert: false, tag: null, close_reason: null, ...over,
})

describe('dispatcher false-alarm panel', () => {
  it('shows the lane stamp in words, age, check-in status and the LOW verification countdown', () => {
    render(<FalseAlarmPanel inc={inc()} fa={fa()} onChanged={() => {}} />)
    expect(screen.getAllByText('lane Low').length).toBeGreaterThan(0)
    expect(screen.getByText('check-in 2/3')).toBeInTheDocument()
    expect(screen.getByText(/verifying for/i)).toHaveTextContent('31s')
    expect(screen.getByText(/age/i)).toHaveTextContent(/01:0\d/)
    expect(screen.getByText(/never dismissed/)).toBeInTheDocument()
  })

  it('Why expands a ruled table whose rows sum to the logit and lists unknown signals as not counted against', async () => {
    render(<FalseAlarmPanel inc={inc()} fa={fa()} onChanged={() => {}} />)
    expect(screen.queryByRole('table')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Why' }))
    const table = screen.getByRole('table')
    expect(within(table).getByText(/model fa-v1-heuristic · config abcd1234abcd1234/)).toBeInTheDocument()
    const rows = within(table).getAllByRole('row')
    expect(within(rows[2]).getByText('touch random')).toBeInTheDocument()            // largest contribution first (-1.44)
    expect(within(table).getByTitle('unknown never lowers the score')).toHaveTextContent('unavailable')
    expect(screen.getByText(/Unknown \(not counted against\): crash like/)).toBeInTheDocument()
    const adds = within(table).getAllByRole('row').slice(1, -1).map((r) => Number(within(r).getAllByRole('cell').slice(-1)[0].textContent))
    expect(Math.abs(adds.reduce((a, b) => a + b, 0) - -2.34)).toBeLessThan(0.02)   // bias + signals = logit
  })

  it('names a hard override that raised the lane', async () => {
    render(<FalseAlarmPanel inc={inc()} fa={fa({ lane: 'HIGH', overrides: ['crash_like'], hold_remaining_s: null })} onChanged={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: 'Why' }))
    expect(screen.getByText(/Hard override:/).closest('p')).toHaveTextContent('crash like')
  })

  it('Close needs a reason, then posts it; the record is not deleted', async () => {
    const onChanged = vi.fn()
    const api = mockApi({ 'POST /api/incidents/INC-1001/close': { status: 'closed' } })
    render(<FalseAlarmPanel inc={inc()} fa={fa()} onChanged={onChanged} />)
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    const save = screen.getByRole('button', { name: 'Close with reason' })
    expect(save).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Reason \(required\)/), 'caller confirmed safe by phone')
    await userEvent.click(save)
    await waitFor(() => expect(api.find('POST', '/api/incidents/INC-1001/close')[0]?.body).toEqual({ reason: 'caller confirmed safe by phone' }))
    expect(onChanged).toHaveBeenCalled()
  })

  it('Tag outcome posts outcome and reason', async () => {
    const api = mockApi({ 'POST /api/incidents/INC-1001/false-alarm/tag': {} })
    render(<FalseAlarmPanel inc={inc()} fa={fa()} onChanged={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: 'Tag outcome' }))
    await userEvent.selectOptions(screen.getByLabelText(/Outcome/), 'genuine')
    await userEvent.type(screen.getByLabelText(/Reason \(required\)/), 'ambulance found a patient')
    await userEvent.click(screen.getByRole('button', { name: 'Save tag' }))
    await waitFor(() => expect(api.find('POST', '/api/incidents/INC-1001/false-alarm/tag')[0]?.body).toEqual({ outcome: 'genuine', reason: 'ambulance found a patient' }))
  })

  it('Dispatch now is offered only while the LOW hold is running', async () => {
    const api = mockApi({ 'POST /api/incidents/INC-1001/false-alarm/dispatch': {} })
    const { rerender } = render(<FalseAlarmPanel inc={inc()} fa={fa()} onChanged={() => {}} />)
    await userEvent.click(screen.getByRole('button', { name: 'Dispatch now' }))
    await waitFor(() => expect(api.count('POST', '/api/incidents/INC-1001/false-alarm/dispatch')).toBe(1))
    rerender(<FalseAlarmPanel inc={inc()} fa={fa({ hold_remaining_s: null, lane: 'UNCERTAIN' })} onChanged={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Dispatch now' })).toBeNull()
  })

  it('an unanswered check-in raises a visible alert line; caller-says-safe asks for a close or dispatch', () => {
    const { rerender } = render(<FalseAlarmPanel inc={inc()} fa={fa({ checkin: { ...fa().checkin, state: 'unanswered' }, hold_remaining_s: null, lane: 'UNCERTAIN' })} onChanged={() => {}} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/No answer to the check-in/)
    rerender(<FalseAlarmPanel inc={inc()} fa={fa({ awaiting_review: true, hold_remaining_s: null, checkin: { ...fa().checkin, state: 'safe' } })} onChanged={() => {}} />)
    expect(screen.getByText(/Caller says safe/)).toBeInTheDocument()
  })

  it('queue stamps: pending rows say so; lanes are text, not colour alone', () => {
    const { rerender } = render(<LaneStamps inc={inc({ status: 'pending' })} />)
    expect(screen.getByText(/pending · countdown/)).toBeInTheDocument()
    rerender(<LaneStamps inc={inc()} fa={fa({ lane: 'HIGH', checkin: { ...fa().checkin, state: 'none' }, hold_remaining_s: null })} />)
    expect(screen.getByText('lane High')).toBeInTheDocument()
  })
})
