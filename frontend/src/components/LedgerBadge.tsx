import { useState } from 'react'
import { api } from '../lib/api'
import type { LedgerInfo } from '../lib/types'
import { Stamp, type Tone } from './ui'

const TONE: Record<LedgerInfo['ledger_status'], Tone> = { ANCHORED: 'ok', SIGNED: 'info', PENDING: 'mute' }
const WHY: Record<LedgerInfo['ledger_status'], string> = {
  ANCHORED: 'Hash-chained, signed, and covered by a Merkle root anchored outside the database.',
  SIGNED: 'Hash-chained and signed by the actor\'s own device key. Not anchored yet.',
  PENDING: 'Hash-chained and attested by the server key. Waiting for the next anchor.',
}

interface Check {
  ok: boolean
  status: string
  signed_by: string
  merkle_proof_ok: boolean | null
  anchor_provider_ok: boolean | null
  failure: { kind: string; seq: number } | null
}

/** SIGNED / ANCHORED / PENDING stamp for one event, with a one-click check of that event. Text, never colour alone. */
export default function LedgerBadge({ eventId, ledger }: { eventId: string; ledger?: LedgerInfo | null }) {
  const [res, setRes] = useState<Check | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  if (!ledger) return null
  const check = async () => {
    setBusy(true)
    setErr(null)
    try {
      setRes(await api.get<Check>(`/api/ledger/verify/${eventId}`))
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Stamp tone={TONE[ledger.ledger_status]} title={WHY[ledger.ledger_status]}>
        {ledger.ledger_status}
      </Stamp>
      {ledger.client_signed && <Stamp tone="info" title={`Signed on the actor's device with key ${ledger.signer_key_id}`}>device signature</Stamp>}
      {ledger.entry_hash && <span className="font-mono text-[10px] text-rule" title={ledger.entry_hash}>#{ledger.seq} {ledger.entry_hash.slice(0, 10)}</span>}
      <button className="min-h-[24px] border border-rule/50 px-1.5 font-mono text-[10px] uppercase hover:bg-bone hover:text-ink" onClick={check} disabled={busy}>
        {busy ? 'Checking…' : 'Verify'}
      </button>
      {res && (
        <Stamp tone={res.ok ? 'ok' : 'bad'}>
          {res.ok ? `verified${res.merkle_proof_ok ? ' · proof ok' : ''}` : `failed · ${res.failure?.kind ?? 'check'}`}
        </Stamp>
      )}
      {err && <span role="alert" className="text-[11px] text-red">{err}</span>}
    </span>
  )
}
