import { act, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth'
import { backoff, EventsProvider, useEvents } from './events'
import { ageText, dist, etaRange, label, mmss } from './format'
import { ev, FakeWebSocket, installWebSocket, signIn } from '../test/helpers'

function Probe({ onEvent }: { onEvent?: (t: string) => void }) {
  const { link, lastSeq, resyncKey, subscribe } = useEvents()
  subscribe((e) => onEvent?.(e.event_type))
  return (
    <div>
      <span data-testid="link">{link}</span>
      <span data-testid="seq">{lastSeq}</span>
      <span data-testid="resync">{resyncKey}</span>
    </div>
  )
}

function mount(onEvent?: (t: string) => void) {
  signIn({ sub: 'disp-1', role: 'dispatcher', name: 'D' })
  installWebSocket()
  return render(
    <AuthProvider>
      <EventsProvider>
        <Probe onEvent={onEvent} />
      </EventsProvider>
    </AuthProvider>,
  )
}

describe('realtime link: reconnect and resync', () => {
  it('connects, reports live, and tracks the last event sequence', async () => {
    const seen: string[] = []
    mount((t) => seen.push(t))
    await waitFor(() => expect(screen.getByTestId('link')).toHaveTextContent('live'))
    expect(FakeWebSocket.instances[0].url).toContain('/ws/events?token=test-token')
    expect(FakeWebSocket.instances[0].url).not.toContain('since=')
    act(() => FakeWebSocket.instances[0].emit(ev('sync.hello', { seq: 10 })))
    act(() => FakeWebSocket.instances[0].emit(ev('incident.created', { seq: 11 })))
    expect(screen.getByTestId('seq')).toHaveTextContent('11')
    expect(seen).toContain('incident.created')
  })

  it('after a drop it shows reconnecting, then reconnects asking for events since the last seq', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mount()
    await vi.waitFor(() => expect(screen.getByTestId('link')).toHaveTextContent('live'))
    act(() => FakeWebSocket.instances[0].emit(ev('x.y', { seq: 42 })))
    act(() => FakeWebSocket.instances[0].drop())
    expect(screen.getByTestId('link')).toHaveTextContent('reconnecting')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(FakeWebSocket.instances.length).toBe(2)
    expect(FakeWebSocket.instances[1].url).toContain('since=42')
    await vi.waitFor(() => expect(screen.getByTestId('link')).toHaveTextContent('live'))
    vi.useRealTimers()
  })

  it('when the server says the gap is too big the app must reload REST state (resync key changes)', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('link')).toHaveTextContent('live'))
    expect(screen.getByTestId('resync')).toHaveTextContent('0')
    act(() => FakeWebSocket.instances[0].emit(ev('sync.resync_required', { seq: 99 })))
    expect(screen.getByTestId('resync')).toHaveTextContent('1')
  })

  it('backoff grows and is capped', () => {
    expect(backoff(1)).toBeLessThan(backoff(2))
    expect(backoff(20)).toBe(10000)
  })
})

describe('formatting', () => {
  it('ages, ETA ranges, distances', () => {
    expect(ageText(5)).toBe('5s')
    expect(ageText(125)).toBe('2m 05s')
    expect(ageText(null)).toBe('never')
    expect(etaRange(240, 400)).toBe('4-7 min')
    expect(etaRange(300, 300)).toBe('5 min')
    expect(etaRange(null, 1)).toBe('ETA unknown')
    expect(dist(540)).toBe('540 m')
    expect(dist(1500)).toBe('1.5 km')
    expect(mmss(75)).toBe('01:15')
    expect(label('exact_location')).toBe('exact location')
  })
})
