import { useCallback, useEffect, useRef, useState } from 'react'
import { ErrorBox, Section, Stamp } from '../../components/ui'
import { api, ApiError, getToken } from '../../lib/api'
import { useAuth } from '../../lib/auth'
import { signerInfo, signerState } from '../../lib/keys'

interface Head {
  entries: number
  head_hash: string | null
  anchors: number
  last_anchor: { anchor_id: string; to_seq: number; merkle_root: string } | null
  provider: string
  provider_label: string
  anchor_every_n: number
}
interface Failure {
  seq: number
  kind: string
  expected: unknown
  found: unknown
  detail: string
}
interface Result {
  ok: boolean
  status: 'VERIFIED' | 'TAMPERED'
  entries: number
  legacy_unchained: number
  client_signed: number
  server_signed: number
  anchors: { checked: number; total: number; provider: string; label: string }
  failure: Failure | null
}
interface Progress {
  phase: string
  checked: number
  total: number
}
interface Tamper {
  mode: string
  what: string
  detected: boolean
  failure: Failure | null
  sandbox_entries: number
  real_chain_untouched: boolean
  label: string
}

const MODES = [
  ['modify', 'Change an entry'],
  ['delete', 'Delete an entry'],
  ['reorder', 'Swap two entries'],
  ['insert', 'Insert a forged entry'],
  ['truncate', 'Cut off the newest entries'],
] as const

const short = (v: unknown) => (typeof v === 'string' && v.length > 20 ? `${v.slice(0, 10)}…${v.slice(-6)}` : String(v ?? '—'))

function FailureView({ f }: { f: Failure }) {
  return (
    <div role="alert" className="mt-3 border-l-4 border-red bg-red/10 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Stamp tone="bad">tampered at seq {f.seq}</Stamp>
        <span className="font-mono text-xs uppercase">{f.kind.replace(/_/g, ' ')}</span>
      </div>
      <p className="mt-1 text-sm">{f.detail}</p>
      <table className="mt-2 w-full border-collapse text-xs">
        <tbody>
          <tr className="border-b hair"><td className="w-24 py-1 font-mono uppercase text-rule">Expected</td><td className="font-mono" title={String(f.expected)}>{short(f.expected)}</td></tr>
          <tr className="border-b hair"><td className="py-1 font-mono uppercase text-rule">Found</td><td className="font-mono" title={String(f.found)}>{short(f.found)}</td></tr>
        </tbody>
      </table>
    </div>
  )
}

/** Verify Integrity: one click re-checks every hash link, every signature and every anchor root, with streaming progress. */
export default function LedgerPage() {
  const { user } = useAuth()
  const [head, setHead] = useState<Head | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [result, setResult] = useState<Result | null>(null)
  const [demo, setDemo] = useState<Tamper | null>(null)
  const [demoBusy, setDemoBusy] = useState<string | null>(null)
  const [anchoring, setAnchoring] = useState(false)
  const abort = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    try {
      setHead(await api.get<Head>('/api/ledger/head'))
      setErr(null)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not load the ledger')
    }
  }, [])
  useEffect(() => {
    void load()
    return () => abort.current?.abort()
  }, [load])

  const verify = async () => {
    setRunning(true)
    setResult(null)
    setProgress({ phase: 'starting', checked: 0, total: head?.entries ?? 0 })
    setErr(null)
    abort.current = new AbortController()
    try {
      const res = await fetch('/api/ledger/verify', { method: 'POST', headers: { Authorization: `Bearer ${getToken() ?? ''}` }, signal: abort.current.signal })
      if (!res.ok || !res.body) throw new ApiError(res.status, 'Verification failed to start')
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let i: number
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim()
          buf = buf.slice(i + 1)
          if (!line) continue
          const ev = JSON.parse(line) as { type: string } & Partial<Progress> & Partial<Result>
          if (ev.type === 'progress') setProgress({ phase: ev.phase ?? '', checked: ev.checked ?? 0, total: ev.total ?? 0 })
          else if (ev.type === 'result') setResult(ev as Result)
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') setErr(e instanceof ApiError ? e.message : 'Verification was interrupted. Try again.')
    } finally {
      setRunning(false)
    }
  }

  const runDemo = async (mode: string) => {
    setDemoBusy(mode)
    setErr(null)
    try {
      setDemo(await api.post<Tamper>('/api/ledger/tamper-demo', { mode }))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Demonstration failed')
    } finally {
      setDemoBusy(null)
    }
  }
  const anchorNow = async () => {
    setAnchoring(true)
    try {
      await api.post('/api/ledger/anchor')
      await load()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not anchor')
    } finally {
      setAnchoring(false)
    }
  }

  const pct = progress && progress.total ? Math.min(100, Math.round((progress.checked / progress.total) * 100)) : running ? 3 : 0
  const signer = signerInfo()
  return (
    <div className="mx-auto max-w-5xl">
      <div className="border-b hair px-4 py-3">
        <h1 className="font-display text-4xl font-bold uppercase leading-none">Verify integrity</h1>
        <p className="mt-1 max-w-3xl text-sm text-rule">
          Every event is chained to the one before it, signed, and periodically anchored by a Merkle root outside the database. This page re-checks all of it. Simulated data; the local anchor is not a public blockchain.
        </p>
      </div>
      {err && <div className="px-4 pt-3"><ErrorBox message={err} onRetry={load} /></div>}

      <Section title="Ledger" right={head && <Stamp tone="mute">{head.provider}</Stamp>}>
        {!head ? (
          <p className="text-sm text-rule">Loading…</p>
        ) : (
          <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Entries</dt><dd className="font-mono">{head.entries}</dd></div>
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Anchors</dt><dd className="font-mono">{head.anchors}{head.last_anchor ? ` · last covers #${head.last_anchor.to_seq}` : ''}</dd></div>
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Head hash</dt><dd className="font-mono" title={head.head_hash ?? ''}>{head.head_hash ? head.head_hash.slice(0, 16) + '…' : 'empty'}</dd></div>
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Anchor provider</dt><dd className="text-right">{head.provider_label}</dd></div>
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Anchors every</dt><dd className="font-mono">{head.anchor_every_n} entries</dd></div>
            <div className="flex justify-between border-b hair py-1"><dt className="font-mono text-[11px] uppercase text-rule">Your signing key</dt><dd className="font-mono">{signerState() === 'unlocked' && signer ? signer.keyId : 'not available (actions are unsigned)'}</dd></div>
          </dl>
        )}
        {user?.role === 'admin' && (
          <button className="btn-ghost mt-3 !min-h-[40px]" onClick={anchorNow} disabled={anchoring}>{anchoring ? 'Anchoring…' : 'Anchor now'}</button>
        )}
      </Section>

      <Section title="Check everything" right={<span className="font-mono text-[10px] uppercase text-rule">hash links · signatures · anchor roots</span>}>
        <button className="btn-primary" onClick={verify} disabled={running}>{running ? 'Verifying…' : 'Verify integrity'}</button>
        {(running || progress) && (
          <div className="mt-3">
            <div className="flex justify-between font-mono text-xs uppercase text-rule">
              <span>{progress?.phase}</span>
              <span>{progress ? `${progress.checked} / ${progress.total}` : ''}</span>
            </div>
            <div role="progressbar" aria-label="Verification progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} className="mt-1 h-3 border border-rule/60">
              <div className="h-full bg-orange" style={{ width: `${pct}%` }} />
            </div>
          </div>
        )}
        {result && (
          <div className="mt-3" data-testid="verify-result">
            <div className="flex flex-wrap items-center gap-3">
              <span className={`stamp !text-lg ${result.ok ? 'border-green text-green' : 'border-red text-red'}`}>{result.ok ? 'VERIFIED' : 'TAMPERED'}</span>
              <span className="text-sm">
                {result.entries} entries checked · {result.client_signed} signed on devices · {result.server_signed} attested by the server · {result.anchors.checked} of {result.anchors.total} anchors match
              </span>
            </div>
            {result.ok ? (
              <p className="mt-1 text-sm text-green">No entry was changed, removed, inserted or reordered, every signature verifies, and every anchored root matches.</p>
            ) : (
              result.failure && <FailureView f={result.failure} />
            )}
          </div>
        )}
      </Section>

      <Section title="Tamper demo" right={<Stamp tone="warn">demonstration</Stamp>}>
        <p className="max-w-3xl text-sm">
          Runs on a throw-away <b>copy</b> of the chain in memory. The real ledger is never changed. It shows what the check above would report if someone altered the history.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {MODES.map(([m, l]) => (
            <button key={m} className="btn-ghost !min-h-[40px] !px-3 !text-base" onClick={() => runDemo(m)} disabled={demoBusy !== null}>{demoBusy === m ? 'Running…' : l}</button>
          ))}
        </div>
        {demo && (
          <div className="mt-3" data-testid="tamper-result">
            <div className="flex flex-wrap items-center gap-2">
              <Stamp tone="warn">demonstration on a copy</Stamp>
              <Stamp tone={demo.detected ? 'ok' : 'bad'}>{demo.detected ? 'detected' : 'NOT detected'}</Stamp>
              <Stamp tone={demo.real_chain_untouched ? 'ok' : 'bad'}>{demo.real_chain_untouched ? 'real ledger unchanged' : 'real ledger changed'}</Stamp>
            </div>
            <p className="mt-1 text-sm">The copy: {demo.what}.</p>
            {demo.failure ? <FailureView f={demo.failure} /> : <p className="mt-1 text-sm text-amber">This change cannot be caught by hash links alone. Anchoring is what catches it.</p>}
            <p className="mt-2 font-mono text-[10px] text-rule">{demo.label}</p>
          </div>
        )}
      </Section>
    </div>
  )
}
