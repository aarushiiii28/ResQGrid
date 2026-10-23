import { useEffect, useState } from 'react'
import { Empty, ErrorBox, Section, Stamp } from '../../components/ui'
import { api, ApiError } from '../../lib/api'
import { verifyMerkleProof, verifyReceipt, type ProofStep, type Receipt, type ReceiptCheck } from '../../lib/ledgerCrypto'
import { listReceipts, removeReceipt, type StoredReceipt } from '../../lib/receipts'

interface Proof {
  anchored: boolean
  status: string
  entry_hash: string
  merkle_root?: string
  path?: ProofStep[]
  anchor?: { anchor_id: string; provider: string }
  provider_label?: string
  note?: string
}

/** Verify a receipt: the server's signature is checked in this browser, with no server round trip. Once the event is
 * anchored, a Merkle proof ties it to a root held outside the database, also checked here. */
export default function VerifyReceiptPage() {
  const [receipts, setReceipts] = useState<StoredReceipt[]>(listReceipts())
  const [text, setText] = useState('')
  const [parsed, setParsed] = useState<Receipt | null>(null)
  const [trusted, setTrusted] = useState('')
  const [check, setCheck] = useState<ReceiptCheck | null>(null)
  const [proof, setProof] = useState<{ p: Proof; ok: boolean | null; hashMatches: boolean } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!parsed && receipts.length) {
      setParsed(receipts[0])
      setText(JSON.stringify(receipts[0], null, 2))
    }
    // first load only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const parse = (raw: string) => {
    setText(raw)
    setCheck(null)
    setProof(null)
    setErr(null)
    if (!raw.trim()) return setParsed(null)
    try {
      const r = JSON.parse(raw) as Receipt
      if (!r.event_id || !r.server_signature || !r.message) throw new Error('not a receipt')
      setParsed(r)
    } catch {
      setParsed(null)
      setErr('That is not a valid receipt. Paste the JSON exactly as exported.')
    }
  }

  const fetchKey = async () => {
    setBusy(true)
    try {
      setTrusted((await api.get<{ public_key: string }>('/api/ledger/server-key')).public_key)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not reach the server')
    } finally {
      setBusy(false)
    }
  }

  const verifyOffline = () => {
    if (parsed) setCheck(verifyReceipt(parsed, trusted.trim() || undefined))
  }

  const getProof = async () => {
    if (!parsed) return
    setBusy(true)
    setErr(null)
    try {
      const p = await api.get<Proof>(`/api/ledger/proof/${parsed.event_id}`)
      const ok = p.anchored && p.path && p.merkle_root ? verifyMerkleProof(p.entry_hash, p.path, p.merkle_root) : null
      setProof({ p, ok, hashMatches: p.entry_hash === parsed.event_hash })
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not fetch the proof')
    } finally {
      setBusy(false)
    }
  }

  const exportJson = () => {
    if (!parsed) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(parsed, null, 2)], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `receipt-${parsed.incident_id}-${parsed.seq}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="mx-auto max-w-4xl">
      <div className="border-b hair px-4 py-3">
        <h1 className="font-display text-4xl font-bold uppercase leading-none">Verify receipt</h1>
        <p className="mt-1 max-w-3xl text-sm text-rule">
          When you press SOS the server signs a receipt that says it received your request and where it sits in the ledger. Keep it. You can check it here later, even offline. Simulated data.
        </p>
      </div>
      {err && <div className="px-4 pt-3"><ErrorBox message={err} /></div>}

      <Section title="Receipts on this device" right={<span className="font-mono text-[10px] uppercase text-rule">{receipts.length} saved</span>}>
        {receipts.length === 0 ? (
          <Empty title="No receipts yet" hint="Press SOS as a caller and the signed receipt is saved here. Or paste one below." />
        ) : (
          <ul>
            {receipts.map((r) => (
              <li key={r.event_id} className="flex flex-wrap items-center gap-3 border-b hair py-1.5 text-sm">
                <button className="font-mono font-semibold underline" onClick={() => parse(JSON.stringify(r, null, 2))}>{r.incident_id}</button>
                <span className="font-mono text-xs text-rule">#{r.seq} · {r.event_type} · {new Date(r.received_at).toLocaleString()}</span>
                <button className="ml-auto font-mono text-[11px] uppercase text-rule underline" onClick={() => { removeReceipt(r.event_id); setReceipts(listReceipts()) }}>
                  Remove from this device
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Receipt">
        <label className="block font-mono text-[10px] uppercase text-rule">
          Receipt JSON
          <textarea aria-label="Receipt JSON" value={text} onChange={(e) => parse(e.target.value)} rows={8} className="mt-0.5 w-full border border-rule/60 bg-ink p-2 font-mono text-xs normal-case" />
        </label>
        <label className="mt-2 block font-mono text-[10px] uppercase text-rule">
          Trusted server public key (optional, base64)
          <div className="mt-0.5 flex gap-2">
            <input aria-label="Trusted server key" value={trusted} onChange={(e) => setTrusted(e.target.value)} className="min-h-[40px] flex-1 border border-rule/60 bg-ink px-2 font-mono text-xs normal-case" placeholder="Leave empty to use the key printed in the receipt" />
            <button className="btn-ghost !min-h-[40px] !px-3 !text-base" onClick={fetchKey} disabled={busy}>Fetch from server</button>
          </div>
        </label>
        <p className="mt-1 text-xs text-rule">A key printed inside a receipt proves nothing on its own. For a real check, use a server key you got from a source you trust.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn-primary" onClick={verifyOffline} disabled={!parsed}>Verify offline</button>
          <button className="btn-ghost" onClick={getProof} disabled={!parsed || busy}>Check Merkle proof</button>
          <button className="btn-ghost" onClick={exportJson} disabled={!parsed}>Export JSON</button>
        </div>

        {check && (
          <div className="mt-3" data-testid="receipt-result">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`stamp !text-lg ${check.ok ? 'border-green text-green' : 'border-red text-red'}`}>{check.ok ? 'SIGNATURE VALID' : 'NOT VALID'}</span>
              <Stamp tone={check.message_matches ? 'ok' : 'bad'}>fields {check.message_matches ? 'match' : 'differ'}</Stamp>
              <Stamp tone={check.signature ? 'ok' : 'bad'}>server signature {check.signature ? 'verifies' : 'fails'}</Stamp>
            </div>
            {check.reasons.length > 0 && <ul className="mt-1 list-disc pl-5 text-sm text-red">{check.reasons.map((x) => <li key={x}>{x}</li>)}</ul>}
            {check.ok && parsed && (
              <p className="mt-1 text-sm">
                The server received <b className="font-mono">{parsed.event_type}</b> for <b className="font-mono">{parsed.incident_id}</b> at {new Date(parsed.received_at).toLocaleString()}, as ledger entry <b className="font-mono">#{parsed.seq}</b>.
              </p>
            )}
          </div>
        )}
        {proof && (
          <div className="mt-3" data-testid="proof-result">
            <div className="flex flex-wrap items-center gap-2">
              <Stamp tone={proof.p.anchored ? 'ok' : 'mute'}>{proof.p.status}</Stamp>
              <Stamp tone={proof.hashMatches ? 'ok' : 'bad'}>entry hash {proof.hashMatches ? 'matches the receipt' : 'differs from the receipt'}</Stamp>
              {proof.ok !== null && <Stamp tone={proof.ok ? 'ok' : 'bad'}>merkle proof {proof.ok ? 'verified here' : 'fails'}</Stamp>}
            </div>
            <p className="mt-1 text-sm">
              {proof.p.anchored
                ? `Covered by ${proof.p.anchor?.anchor_id}, anchored with ${proof.p.provider_label}.`
                : proof.p.note ?? 'Not anchored yet. Check again after the next anchor.'}
            </p>
          </div>
        )}
      </Section>
    </div>
  )
}
