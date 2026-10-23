import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import LedgerBadge from '../../components/LedgerBadge'
import { AuthProvider } from '../../lib/auth'
import { EventsProvider } from '../../lib/events'
import { V } from '../../test/vectors'
import { installWebSocket, mockApi, signIn } from '../../test/helpers'
import LedgerPage from './LedgerPage'
import VerifyReceiptPage from './VerifyReceiptPage'

const head = { entries: 42, head_hash: 'ab'.repeat(32), anchors: 1, last_anchor: { anchor_id: 'ANC-0001', to_seq: 30, merkle_root: 'cd'.repeat(32) },
  provider: 'local', provider_label: 'local anchor, not a public blockchain', anchor_every_n: 25 }
const ok = { type: 'result', ok: true, status: 'VERIFIED', entries: 42, legacy_unchained: 0, client_signed: 5, server_signed: 37, failure: null,
  anchors: { checked: 1, total: 1, provider: 'local', label: 'local anchor, not a public blockchain' } }
const bad = { ...ok, ok: false, status: 'TAMPERED', failure: { seq: 17, kind: 'entry_hash', expected: 'e'.repeat(64), found: 'f'.repeat(64), detail: 'the entry was changed after it was written' } }
const ndjson = (...evs: object[]) => new Response(evs.map((e) => JSON.stringify(e)).join('\n') + '\n', { status: 200, headers: { 'content-type': 'application/x-ndjson' } })

function renderPage(el: ReactElement, role: 'dispatcher' | 'admin' | 'caller' = 'dispatcher', extra: Record<string, unknown> = {}) {
  signIn({ sub: role === 'admin' ? 'admin-1' : 'disp-1', role, name: 'U' })
  installWebSocket()
  const api = mockApi({ 'GET /api/ledger/head': head, ...extra })
  render(<MemoryRouter><AuthProvider><EventsProvider>{el}</EventsProvider></AuthProvider></MemoryRouter>)
  return api
}

describe('verify integrity page', () => {
  it('shows the ledger and an honest anchor label', async () => {
    renderPage(<LedgerPage />)
    expect(await screen.findByText('local anchor, not a public blockchain')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText(/last covers #30/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Anchor now' })).toBeNull()                  // admins only
  })

  it('one click verifies with streaming progress and ends in a VERIFIED stamp (text, not colour alone)', async () => {
    renderPage(<LedgerPage />)
    await screen.findByText('42')
    vi.stubGlobal('fetch', vi.fn(async (url: string) => (String(url) === '/api/ledger/verify'
      ? ndjson({ type: 'start', total: 42 }, { type: 'progress', phase: 'links and signatures', checked: 21, total: 42 }, { type: 'progress', phase: 'anchors', checked: 1, total: 1 }, ok)
      : new Response(JSON.stringify(head), { headers: { 'content-type': 'application/json' } }))))
    await userEvent.click(screen.getByRole('button', { name: 'Verify integrity' }))
    const res = await screen.findByTestId('verify-result')
    expect(within(res).getByText('VERIFIED')).toBeInTheDocument()
    expect(res).toHaveTextContent(/42 entries checked · 5 signed on devices · 37 attested by the server · 1 of 1 anchors match/)
    expect(screen.getByRole('progressbar', { name: 'Verification progress' })).toBeInTheDocument()
  })

  it('a tampered chain shows TAMPERED at the sequence number with expected versus found', async () => {
    renderPage(<LedgerPage />)
    await screen.findByText('42')
    vi.stubGlobal('fetch', vi.fn(async () => ndjson({ type: 'start', total: 42 }, bad)))
    await userEvent.click(screen.getByRole('button', { name: 'Verify integrity' }))
    const res = await screen.findByTestId('verify-result')
    expect(within(res).getByText('TAMPERED')).toBeInTheDocument()
    const alert = within(res).getByRole('alert')
    expect(alert).toHaveTextContent('tampered at seq 17')
    expect(alert).toHaveTextContent('entry hash')
    expect(alert).toHaveTextContent(/Expected\s*eeeeeeeeee/)
    expect(alert).toHaveTextContent(/Found\s*ffffffffff/)
  })

  it('a failed request shows an error instead of nothing', async () => {
    renderPage(<LedgerPage />)
    await screen.findByText('42')
    vi.stubGlobal('fetch', vi.fn(async () => new Response('x', { status: 500 })))
    await userEvent.click(screen.getByRole('button', { name: 'Verify integrity' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/failed to start|HTTP 500/i)
  })

  it('the tamper demo is labelled as a demonstration on a copy and reports the real ledger unchanged', async () => {
    const api = renderPage(<LedgerPage />, 'dispatcher', { 'POST /api/ledger/tamper-demo': {
      mode: 'delete', what: 'deleted entry 21', detected: true, sandbox_entries: 41, real_chain_untouched: true,
      failure: { seq: 21, kind: 'seq_gap', expected: 21, found: 22, detail: 'an entry is missing, moved or inserted' },
      label: 'DEMONSTRATION on a throw-away copy in memory. The real ledger was not changed.' } })
    await screen.findByText('42')
    expect(screen.getAllByText(/demonstration/i).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: 'Delete an entry' }))
    const out = await screen.findByTestId('tamper-result')
    expect(api.find('POST', '/api/ledger/tamper-demo')[0].body).toEqual({ mode: 'delete' })
    expect(out).toHaveTextContent('demonstration on a copy')
    expect(out).toHaveTextContent('detected')
    expect(out).toHaveTextContent('real ledger unchanged')
    expect(out).toHaveTextContent('tampered at seq 21')
  })

  it('admins can anchor now', async () => {
    const api = renderPage(<LedgerPage />, 'admin', { 'POST /api/ledger/anchor': { anchor_id: 'ANC-0002' } })
    await userEvent.click(await screen.findByRole('button', { name: 'Anchor now' }))
    await waitFor(() => expect(api.count('POST', '/api/ledger/anchor')).toBe(1))
  })
})

describe('verify receipt page', () => {
  it('checks a server-signed receipt in the browser, rejects an edited one, and can verify a Merkle proof offline', async () => {
    localStorage.setItem('resqgrid.receipts', JSON.stringify([{ ...V.receipt, stored_at: new Date().toISOString() }]))
    renderPage(<VerifyReceiptPage />, 'caller', {
      'GET /api/ledger/server-key': { key_id: 'server-1', public_key: V.receipt.server_public_key },
      [`GET /api/ledger/proof/${V.receipt.event_id}`]: { anchored: true, status: 'ANCHORED', entry_hash: V.leaf, merkle_root: V.root, path: V.proof, anchor: { anchor_id: 'ANC-0001', provider: 'local' }, provider_label: 'local anchor, not a public blockchain' },
    })
    expect(screen.getByText('1 saved')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Fetch from server' }))
    await waitFor(() => expect(screen.getByLabelText('Trusted server key')).toHaveValue(V.receipt.server_public_key))
    await userEvent.click(screen.getByRole('button', { name: 'Verify offline' }))
    const res = await screen.findByTestId('receipt-result')
    expect(res).toHaveTextContent('SIGNATURE VALID')
    expect(res).toHaveTextContent(/ledger entry #12/)

    await userEvent.click(screen.getByRole('button', { name: 'Check Merkle proof' }))
    const proof = await screen.findByTestId('proof-result')
    expect(proof).toHaveTextContent('ANCHORED')
    expect(proof).toHaveTextContent('merkle proof verified here')
    expect(proof).toHaveTextContent(/differs from the receipt/)                      // vector leaf is not this receipt's hash: reported, not hidden

    const box = screen.getByLabelText('Receipt JSON') as HTMLTextAreaElement
    await userEvent.clear(box)
    await userEvent.click(box)
    await userEvent.paste(JSON.stringify({ ...V.receipt, seq: 13 }))
    await userEvent.click(screen.getByRole('button', { name: 'Verify offline' }))
    expect(await screen.findByTestId('receipt-result')).toHaveTextContent('NOT VALID')
  })

  it('says so when the pasted text is not a receipt, and when there are no receipts yet', async () => {
    renderPage(<VerifyReceiptPage />, 'caller')
    expect(screen.getByText('No receipts yet')).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Receipt JSON'))
    await userEvent.paste('{"hello": 1}')
    expect(await screen.findByRole('alert')).toHaveTextContent(/not a valid receipt/)
    expect(screen.getByRole('button', { name: 'Verify offline' })).toBeDisabled()
  })
})

describe('ledger badge', () => {
  const info = (status: 'ANCHORED' | 'SIGNED' | 'PENDING', extra = {}) => ({ seq: 7, entry_hash: 'ab'.repeat(32), signer_key_id: 'k-1', client_signed: status === 'SIGNED', anchor_id: status === 'ANCHORED' ? 'ANC-0001' : null, ledger_status: status, ...extra })

  it('spells out ANCHORED, SIGNED and PENDING', () => {
    const { rerender } = render(<LedgerBadge eventId="e1" ledger={info('ANCHORED')} />)
    expect(screen.getByText('ANCHORED')).toBeInTheDocument()
    rerender(<LedgerBadge eventId="e1" ledger={info('SIGNED')} />)
    expect(screen.getByText('SIGNED')).toBeInTheDocument()
    expect(screen.getByText('device signature')).toBeInTheDocument()
    rerender(<LedgerBadge eventId="e1" ledger={info('PENDING')} />)
    expect(screen.getByText('PENDING')).toBeInTheDocument()
  })

  it('Verify checks that one event', async () => {
    mockApi({ 'GET /api/ledger/verify/e1': { ok: true, status: 'ANCHORED', signed_by: 'server', merkle_proof_ok: true, anchor_provider_ok: true, failure: null } })
    render(<LedgerBadge eventId="e1" ledger={info('ANCHORED')} />)
    await userEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText(/verified · proof ok/)).toBeInTheDocument()
  })

  it('renders nothing for an event without ledger data', () => {
    const { container } = render(<LedgerBadge eventId="e1" ledger={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
