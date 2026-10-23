# ResQGrid

Simulated emergency-dispatch prototype. Synthetic data only. No real calls, SMS or integrations. In a real emergency dial 112.

Rules and design system: [CLAUDE.md](CLAUDE.md). Spec: `docs/ResQGrid_Python_FastAPI_Claude_Specification.pdf`.

## Run it

Needs Python 3.12+, Node 20+ and PostgreSQL 14+ (databases `resqgrid` and `resqgrid_test`).

```powershell
copy .env.example .env      # set DATABASE_URL / TEST_DATABASE_URL (password)
.\dev.cmd                   # installs what is missing, starts the API and the web app
```

- Web app: http://localhost:5180
- API docs: http://localhost:8010/docs
- Health: http://localhost:8010/health

Manual start: `venv\Scripts\python.exe scripts\start_backend.py` and `cd frontend; npm run dev`.
On Windows start the API with that script, not bare `uvicorn`: psycopg needs a selector event loop.

## Try the whole flow

1. Landing page: enter as **Caller**. **Press and hold SOS** for 2 seconds, then let the 10 second countdown run (or press Cancel: the record is kept and the caller is checked in on). The call opens, the nearest capable ambulance is chosen and approved by **Demo Autopilot** (labelled simulated), and the hospital gets a pre-alert.
2. In another browser window enter as **Dispatcher**: board, Dispatch Compare, passport timeline. Use **Close road** on the map to force a reroute.
3. Enter as **Driver** (pick the assigned unit): accept, en route, simulate movement (and **Simulate a wrong turn**), on scene, transporting, arrived.
4. Enter as **Hospital** (pick the destination): acknowledge, handover started, handover complete.

Dispatcher extras: **Integrity** (verify the whole ledger, tamper demo on a copy), **Passport** (SIGNED, ANCHORED or PENDING per event), **Replay lab** (dispatch policies, false alarms, stable routes). Caller: **Receipts** (check a server-signed receipt offline).

The Caller screen has a **Demo mode** switch that answers the call-taker for you. A scripted crew drives the unit when no driver app is open; set `SIM_DRIVER=false` to drive it yourself.

## Checks

```powershell
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m pytest                 # API, services, failure and authorization tests
cd frontend
npm run build
npm test                                          # Vitest component tests
npm run test:e2e                                  # Playwright: SOS to handover, signed SOS and ledger, wrong turn
```

Optional scripts: `scripts\recalibrate_false_alarm.py` (fit candidate weights on the synthetic mix; needs admin approval to activate), `scripts\apply_pg_roles.py` (append-only database role), `scripts\deploy_anchor_contract.py` (EVM anchor contract, optional). Docs: [docs/MODELS.md](docs/MODELS.md), [docs/LEDGER.md](docs/LEDGER.md), [docs/FABRIC.md](docs/FABRIC.md), [docs/DEMO.md](docs/DEMO.md).

## Layout

`backend/app`: `api/routes`, `core`, `schemas`, `services`, `optimization`, `routing`, `db`, `websockets`.
`frontend/src`: `pages/{caller,command,driver,hospital,audit}`, `components`, `lib`.

No API keys are needed. OR-Tools is a normal dependency. The LLM extraction adapter stays off unless `LLM_EXTRACTION=true` and `ANTHROPIC_API_KEY` are set.

## Attribution

Map data © OpenStreetMap contributors (tiles under ODbL). Routing: OSRM (optional, public demo server) with a deterministic offline fallback. Maps: Leaflet, react-leaflet, leaflet.heat. Optimisation: Google OR-Tools. Fonts: Barlow Condensed and IBM Plex, self-hosted via @fontsource (SIL OFL). Icons: Phosphor. Cryptography: the cryptography package (Ed25519, AES-GCM), @noble/ed25519 and @noble/hashes (MIT) in the browser. Configuration: PyYAML. No third-party repository code was reused.

## Demo and scripts

`docs/DEMO.md` has the 3-minute demo script and the limitations. `scripts/seed_demo.py` seeds synthetic data, `scripts/reset_demo.py` resets it, `scripts/run_benchmark.py` runs the simulated benchmark.
