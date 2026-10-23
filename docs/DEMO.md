# ResQGrid demo scripts

Everything on screen is simulated. Start with `.\dev.cmd`, then open http://localhost:5180. Reset between runs with `venv\Scripts\python.exe scripts\reset_demo.py`.
Open four browser windows: Caller, Dispatcher, Driver and Hospital.

## A. The whole workflow (3 minutes)

| Time | Do | Say / show |
|---|---|---|
| 0:00 | Landing page. Enter as **Caller**. | The strip reads DEMO / SIMULATED and "In a real emergency dial 112". The caller does one thing. |
| 0:15 | **Press and hold SOS** until the ring fills (2 s). Do not cancel the 10 s countdown. | A deliberate hold prevents accidental presses. GPS is taken automatically with its accuracy and age. The in-app simulated call opens. Turn on **Demo mode** to answer the call-taker for you. |
| 0:40 | Dispatcher window: **Command**. | The incident is triaged, with a lane stamp and a "Why" table. The nearest capable ambulance and hospital appear in **Dispatch Compare** with reasons. **Demo Autopilot** approves after a short delay and is audited as `demo-autopilot`. |
| 1:00 | Driver window: accept, en route, **Simulate movement**. | Route locked, turn-by-turn steps, voice prompts, simulated GPS (labelled), green corridor. |
| 1:25 | Hospital window. | The pre-alert is already there. Sent is not acknowledged: press Acknowledge, then Arrival and Handover. |
| 1:45 | Command: **Close road** on the route. | The route changes at once (same destination), the dispatcher alert says why and how many minutes, and the driver only taps "Got it". |
| 2:10 | **Mass casualty**: open a record, set triage colours by hand, plan, approve with a reason. | No hospital is overloaded; unverified capacity counts as zero. |
| 2:35 | **Replay lab**: run seed 42, compare P0 and P2, then the 20-seed benchmark. | Same world, three policies. Simulated results, no claim about lives saved. |

## B. The three innovations (3 minutes)

| Time | Do | Say / show |
|---|---|---|
| 0:00 | **False alarms, layer 1.** Caller: tap SOS once (nothing happens). Hold it, then press **Cancel** during the countdown. | A quick tap does nothing. The cancelled request is kept: Dispatcher > Command > "Show closed and cancelled". It is muted, never dispatched, and one "Are you OK?" check-in was queued. Tap **I am safe** on the caller screen. |
| 0:35 | **Layers 2 to 5.** Caller: hold SOS again and let it go live. On the dispatcher's incident press **Why**. | The table shows each signal's evidence, weight and contribution adding up to the score, the model version and config hash. Unknown sensors are listed as unknown and never lower it. A crash signature forces HIGH; a LOW lane only delays dispatch for up to 45 s and a dispatcher can **Dispatch now**. |
| 1:05 | **Replay lab > False alarms.** Run the call mix. | Read the red box first: genuine emergencies left in the LOW lane (6 of 86 on seed 42). Then flagged false alarms, time to verify, dispatcher workload, the threshold sweep and where it fails. |
| 1:25 | **Stable routes.** Driver: Simulate movement, then **Simulate a wrong turn**. | The banner says "Off route. Rejoin in ... m". The route rejoins the original line; the driver taps "Got it". Mild congestion is logged as "kept" with the reasons; heavy sustained congestion reroutes only when it saves at least 60 s and 15 percent. |
| 1:50 | **Replay lab > Stable routes.** Run the comparison. | Three policies on identical trips. The stable policy changes the route less and keeps the ETA steadier. Read "Where the stable policy is worse". |
| 2:10 | **Integrity** (dispatcher). Press **Verify integrity**. | VERIFIED with counts of device-signed and server-attested entries and anchor matches. Open the **Passport**: every event shows SIGNED, ANCHORED or PENDING. |
| 2:30 | Press **Delete an entry** in the **Tamper demo**. | "Demonstration on a copy": TAMPERED at the sequence number with expected versus found, and "real ledger unchanged". The real chain still verifies. |
| 2:45 | Caller: **Receipts**. **Verify offline**. | The server-signed receipt from the SOS press checks in the browser with no server round trip. |

## Benchmark numbers (all simulated)

Dispatch policies, 20 seeds, all faults: `venv\Scripts\python.exe scripts\run_benchmark.py`

| | P0 baseline | P1 | P2 ResQGrid |
|---|---|---|---|
| Median response | 19.6 min | 19.3 min | 15.7 min |
| Meeting requirements | 87.5% | 87.5% | 93.8% |
| Failures detected | 42% | 33% | 87% |

P2 beat P0 on 15 of 20 seeds. P2 is worse on arrival-to-handover (+21 s on 15 of 20 seeds) and made deliberate hand-overs on 8 of 20 seeds. Say this out loud.

False-alarm call mix, seed 42, 300 calls (86 genuine, 214 accidental), threshold LOW below 0.35:
* **6 of 86 genuine emergencies (7.0%) are left in the LOW lane** after the check-in (mostly the silent medical case that looks like a test press); 2 more start LOW and are rescued within 45 s.
* 115 of 214 accidental calls flagged (53.7%); 99 accidental calls are still dispatched.
* Median time to verify 37.3 s; 42 calls per 100 need a dispatcher's eyes.

Stable routes, 14 identical trips (seeds 1 to 14), `Replay lab > Stable routes`:

| | Reroute on every change | Never reroute | Stable policy |
|---|---|---|---|
| Total travel time | 718 s | 1144 s | 727 s |
| Route changes | 2.2 | 0 | 1.6 |
| ETA variability (std dev) | 126 s | 375 s | 82 s |
| Turns changed | 4.4 | 0 | 2.5 |
| Time lost after a wrong turn | 49 s | 79 s | 38 s |

The stable policy is slower than reroute-on-every-change on 3 of 14 trips (by 92 s on average when it is) and changes the route more often than never-reroute (13 of 14 trips) because it reacts to wrong turns and closed roads.

## Limitations

* Synthetic data only: users, hospitals, ambulances, volunteers, AEDs, places and incidents. Nothing is clinically validated.
* No real 112/108/911, SMS, radio, hospital or traffic-signal integration. The call is an in-app simulation; bystander alerts are simulated.
* **The false-alarm model is a heuristic**, not a trained or validated classifier. Its weights are hand-set; the recalibration script fits them on invented data and cannot tell you how the model performs on real calls. Some genuine emergencies will land in the LOW lane (see above); that is why LOW only delays dispatch and a dispatcher can override.
* Microphone features need explicit consent and run only on the phone; no audio is stored or sent. If consent or sensors are missing the score stays at its prior.
* **The local anchor is not a public blockchain.** Whoever controls the machine controls the anchor file. The EVM provider is optional and was **not exercised against a real chain** in this build: `web3` would not install on Python 3.14 here (a native dependency failed to build), so it is tested against a fake only. Fabric is a documented stub.
* **Client-side keys live in browser storage**; the demo passphrase is derived from the user id so the demo needs no setup. A real deployment needs a user-chosen passphrase or a platform secure store. Tail truncation of the ledger is only detected once an anchor covers it.
* Personal location data is encrypted per subject and can be crypto-shredded; other personal data (call text) is not stored in the ledger.
* The route lab is a toy city with constant speeds, invented fluctuating congestion and one vehicle. The stable policy's thresholds were set by reasoning, not tuned on real roads.
* Public OSRM is best effort. When it is down the app falls back to deterministic mock routing and says so.
* Browser GPS can be denied or weak; the app then uses the last known fix plus a manual pin, flagged approximate. Voice prompts and speech input depend on the browser's Web Speech API.
* Single instance, demo auth. Not hardened for production.
