# Models and approximations (Phases 5 to 11)

Everything here runs on synthetic data. None of it is clinically validated and none of it predicts real emergencies.

## M8 Risk monitor and recovery planner
Eight triggers on live plans: stale vehicle GPS, no acknowledgment, route closure, stale hospital resource, capacity change (plus capability change), ETA drift, state conflict, competing incident.
Each becomes one deduplicated risk with evidence, severity, the plan assumption it threatens and options: keep current plan (always), request confirmation, reroute, alternate destination, propose eligible unit, escalate.
- A plan-changing option needs a benefit of at least `min_reassign_benefit` score points unless the trigger makes the current plan unsafe (closure on the route, silent crew, unavailable destination).
- ETA drift uses hysteresis: raised above 1.25x and 60 s, held until it falls under 1.10x.
- After a risk is resolved its key is on cooldown (`risk_cooldown_s`), so the same thing is not re-raised.
- Only a human approves a change. Demo Autopilot may approve a recommended action and is audited as `demo-autopilot`; it leaves keep-plan and escalate to a human.
- A reroute approved here only becomes a proposal on the driver's screen; the driver still taps. Released units are audited, never cancelled silently.
- Since M20 a closed road on the route is handled first by the stable-route policy (same destination, applied automatically, driver acknowledges), so the closure risk is normally not raised. The human-approved mode (admin switch `POST /api/routing/policy`) restores this proposal flow.

## M15 Mass casualty
People set triage colours (red, amber, green, black). Extracted suggestions never count until a person confirms them, and unconfirmed patients are planned as red. Capacity comes only from fresh, available resource counts. The split is a CP-SAT assignment with an increasing cost per extra patient at a hospital (spreads load); reds are never displaced by lower triage. Ambulance loads (one red, or two amber/green) are assigned with OR-Tools and compared with greedy. All rules are configuration in `mci_service.py`, not clinical advice.

## M13 Demand forecast and pre-positioning (prototype approximation)
Seeded synthetic history of 700 incidents from six hotspots. Six 4-hour buckets by weekday or weekend. Counts per ~700 m cell divided by days observed, Gaussian-smoothed with a small prior. Standby points are chosen greedily to maximise demand-weighted coverage within a straight-line response radius; available units are matched to them with OR-Tools (minimum total distance). It is shown as an optional layer in the Command Center.

## M11 Replay Lab
A deterministic event engine (`backend/app/simulation`). One seeded world per seed, replayed by P0, P1 and P2.
- Policies see the world only through an observation view (positions and hospital states as last reported, published closures, crew status messages). They cannot read hidden truth; a test perturbs future events and checks earlier decisions are unchanged.
- P0: nearest unit by straight line, nearest capable hospital, last-known data. P1: product scoring (time, freshness, coverage, uncertainty), duplicate merging, entrance confirmation. P2: P1 plus ack tracking (60 s), a 30 s risk monitor, confirmation before acting on stale data, human approval delay, and priority-gap hand-overs.
- Faults: duplicate calls, stale hospital data, blocked entrance, route closure, no acknowledgment, stale GPS (real outages and false-alarm blips), offline unit, competing incident.
- Limits: a toy city (Manhattan distances, constant speed, log-normal noise), eight hospitals, eleven units, no pre-positioning inside the simulation, a fixed human approval delay, and a dispatcher who is assumed to confirm duplicate merges. Results depend on these choices.
- Results are labelled simulated everywhere and make no claim about lives saved.

## M18 False-alarm detection (heuristic, not clinically validated)
Five layers; the goal is to verify and prioritise, never to dismiss. Nothing is deleted and nothing is auto-closed.
1. **Accidental-press prevention.** SOS needs a 2 s press-and-hold with a progress ring, then a 10 s countdown (beep and haptic each second, one huge Cancel). The countdown creates a PENDING incident: visible to dispatchers as a muted row, not dispatched. Zero reaches `new` and enters the pipeline. Cancel gives `cancelled_by_caller`, kept forever, with one check-in queued. If the app vanishes mid-countdown the server makes the request live after the countdown plus a grace period (fail safe).
2. **Phone signals.** Derived numbers only: crash_like (acceleration spike, then stillness, plus a GPS speed drop), erratic or pocket-like motion, idle device, touch pattern (tap count, interval variance, multi-touch), and microphone shape (loudness, scream band, impact, silence) computed on the device ONLY after explicit consent. No audio is stored or sent; the API rejects unknown fields and the tests check that no audio path exists. A sensor that is unavailable or denied is unknown and never lowers the score.
3. **Context.** Seeded synthetic place type (highway, junction, residential, school, hospital, market), local time, hashed per-install device history of accidental presses, and corroboration by other incidents nearby.
4. **Active check-in.** "Are you OK? Tap if safe" (speech plus button) at T+0, T+15 s (louder), T+30 s. No answer by T+45 s escalates to at least UNCERTAIN and alerts a dispatcher. "I am safe" lowers the score but leaves the incident open for a dispatcher to close with a reason. Words such as help, pain, accident or fire force HIGH; clear all-clear phrases count as a safe answer.
5. **Explainable score.** `logit = bias + sum(weight x evidence)`, evidence in [-1, +1], weights in `backend/config/false_alarm.yaml`. Output: probability_genuine, lane (HIGH at 0.75 and above, LOW below 0.35), per-feature contributions that add up to the logit, the missing-signal list, `model_version` and `config_hash`, persisted with every decision. Hard overrides: crash_like, explicit help and a confirmed danger force HIGH; a scream or an unanswered check-in force at least UNCERTAIN.
LOW lane: dispatch waits at most `low_hold_s` (45 s) while the check-in runs, then goes ahead at least as UNCERTAIN unless the caller said they are safe. A dispatcher can dispatch at once. Dispatchers tag outcomes (accidental, genuine, unknown) with a reason. `scripts/recalibrate_false_alarm.py` fits new weights on the seeded synthetic mix and stores a CANDIDATE with a new version; an admin must approve it (audited) before it is used. No silent model change.
Replay Lab: a seeded call mix (cardiac, crash, assault, fire, silent medical, child taps, pocket presses, test presses, abandoned calls). It reports flagged false alarms, **genuine emergencies left in the LOW lane** (first and prominent), time to verify, dispatcher workload and a threshold sweep, plus where it fails. The hard case is a genuine silent medical call that looks like a deliberate test press.

## M19 Tamper-evident ledger
See [LEDGER.md](LEDGER.md). Hash chain, Ed25519 signatures made on the device, Merkle anchors (local file by default, optional EVM), signed receipts, delete-proofing in the API and in the database role, and crypto-shredding for personal data. The local anchor is not a public blockchain.

## M20 Stable routes
Plan once and commit: a primary route plus up to two backups, each pre-checked for closures (`RoutingProvider.alternatives`, OSRM and a deterministic mock). Each GPS fix is map-matched (`RoutingProvider.match`: OSRM `/match`, or snapping onto known routes for the mock) and measured against the committed route. Thresholds are in `backend/config/routing_policy.yaml`.
- **Wrong turn.** Off route only when the deviation exceeds 45 m on 3 consecutive fixes of good accuracy (30 m or better). Poor accuracy is "uncertain" and changes nothing. Recovery tries to REJOIN the original route at the point ahead (within 1.5 km) with the lowest remaining ETA, and uses a full replan only if it beats the best rejoin by 30 s or 8 percent.
- **Congestion.** Reroute only when ALL hold: remaining ETA up by more than 20 percent and 90 s on two consecutive evaluations; the best alternative saves at least 60 s and 15 percent; at least 60 s since the last reroute; not within the last 300 m. Otherwise keep the route and log why.
- **Blocked road.** Reroute at once, preferring rejoin, even in the final approach.
- Every evaluation is written to the ledger with reason codes, numbers and the policy version. A same-destination change applies automatically, tells the dispatcher with the reason and minutes saved, and the driver acknowledges with one tap (not an approval). A change of destination or vehicle still needs a human.
- Replay Lab compares three policies on identical seeded trips (reroute on every change, never reroute, the stable policy) on route changes, total time, ETA variability, turns changed and time lost after a wrong turn, with a threshold sweep and the places where the stable policy is worse. Limits: a toy city, invented congestion that fluctuates, one vehicle, constant speed. In this model the stable policy is calmer (fewer changes, steadier ETA) at the price of a little speed on some trips.