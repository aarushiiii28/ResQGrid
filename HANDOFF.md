# ResQGrid: session handoff (updated after Phase 11)

## Objective
Simulated emergency dispatch prototype (synthetic data only), built to CLAUDE.md, the spec PDF and the phase prompts 0 to 11. Postgres instead of Mongo, no Docker, built locally with no repo (the user commits later). Run with `.\dev.cmd`, or backend `venv\Scripts\python.exe scripts\start_backend.py` (port 8010) and `cd frontend; npm run dev` (port 5180) in separate terminals.

## Status
Phases 0 to 11 are complete and verified.

| Check | Result |
|---|---|
| ruff | all checks passed |
| pytest | 343 passed |
| frontend build | ok |
| Vitest | 139 passed (17 files) |
| Playwright | 9 passed |

## What was added in Phases 8 to 11
* **M18 false-alarm detection**: hold-to-press and 10 s countdown with pending/cancelled records, device signals (crash, motion, touch, consented mic features), context, check-in ladder, explainable logistic score with lanes, dispatcher tag/close, recalibration script with admin approval, Replay Lab call mix.
  Files: `backend/config/false_alarm.yaml`, `schemas/false_alarm.py`, `services/{false_alarm_model,false_alarm_service}.py`, `api/routes/false_alarm.py`, `simulation/false_alarm_lab.py`, `scripts/recalibrate_false_alarm.py`; frontend `pages/caller/{HoldSos,Countdown,CheckIn,SignalConsent,useSignals,useSos}`, `lib/{signals,alerts}.ts`, `pages/command/FalseAlarmPanel`, `pages/replay/FalseAlarmLab`.
* **M19 ledger**: `audit_events` is the hash-chained, signed ledger; Ed25519 device signatures; Merkle anchors (local file default, EVM/Fabric optional); receipts; delete-proofing (405 plus ledger entry, DB triggers, `scripts/pg_init_roles.sql`); crypto-shredding.
  Files: `services/{ledger_service,crypto_service,merkle}.py`, `services/anchor/*`, `api/routes/ledger.py`, `api/signing.py`, `contracts/ResQGridAnchor.sol`, `docs/{LEDGER,FABRIC}.md`; frontend `lib/{keys,ledgerCrypto,receipts}.ts`, `pages/ledger/{LedgerPage,VerifyReceiptPage}`, `components/LedgerBadge`.
* **M20 stable routes**: `backend/config/routing_policy.yaml`, `services/{routing_policy,route_stability_service}.py`, provider `alternatives` and `match` (mock and OSRM), driver banners, `simulation/route_lab.py`, Replay Lab tab.
* **Phase 11**: end-to-end tests (`test_m18_m20_flow.py`, `e2e/innovations.spec.ts`), a design audit test (`frontend/src/design.audit.test.ts`), `docs/DEMO.md`, `docs/MODELS.md`.

## Important decisions
* Same-destination route changes now apply automatically (driver acknowledges, dispatcher is alerted); destination and vehicle changes still need a human. An admin switch (`POST /api/routing/policy`) restores the older human-approved reroute flow used by the M8 risk monitor.
* Unknown sensors never lower the false-alarm score; LOW lane only delays dispatch by at most 45 s and is never a dismissal.
* The DB credentials live only in the git-ignored `.env`. The server signing key and the local anchor file live in the git-ignored `.runtime/`.

## Known limits and open items
* `web3` would not install on Python 3.14 (a native dependency failed to build), so the EVM anchor provider is tested against a fake only and was never run against a real chain.
* Client keys are in IndexedDB with a demo passphrase (derived from the user id). The local anchor is not a public blockchain.
* The false-alarm model is a hand-set heuristic: on the seeded mix 6 of 86 genuine calls (7.0%) end in the LOW lane. See `docs/DEMO.md`.
* Not tested: a fresh-machine setup from scratch, OSRM-down and GPS-denied in a real browser.

## Next steps (if asked)
1. Run `.\dev.cmd` on a clean machine and fix any setup gaps.
2. Decide whether the LOW-lane miss rate is acceptable, or lower `lanes.low_max` in `false_alarm.yaml`.
3. Commit (the user does this).
