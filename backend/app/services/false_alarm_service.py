"""M18 false-alarm detection, all five layers. The goal is to verify and prioritise, never to dismiss:
nothing is deleted, nothing is auto-closed, and a LOW lane only delays dispatch for a short, bounded
verification window (config `low_hold_s`). Prototype heuristic, not clinically validated.

Layer 1 pending incident -> countdown -> live / cancelled_by_caller (kept forever)
Layer 2 phone signals (derived numbers only, no audio leaves the device)
Layer 3 context (place, time, device history, corroboration)
Layer 4 check-in ladder
Layer 5 explainable score + lanes (see false_alarm_model.py)"""

import math
import re
from datetime import timedelta

from app.core.geo import haversine_m
from app.core.security import Principal
from app.db.repos import Repo
from app.schemas.call import CallSession
from app.schemas.false_alarm import (
    DEVICE_FEATURES,
    LANE_RANK,
    Assessment,
    CheckIn,
    FalseAlarmRecord,
    Feature,
    ModelProposal,
    OutcomeTag,
    Prompt,
    SignalBatch,
)
from app.schemas.incident import Incident
from app.schemas.vehicle import VehicleStateUpdate  # noqa: F401  (also used by recall_dispatch)
from app.services import false_alarm_model as fam
from app.services.incident_service import INACTIVE, RuleViolation, vis_for

HELP_RE = re.compile(r"\b(help|pain|accident|fire|bleed\w*|hurt|dying|attack\w*|chest|unconscious|collapsed|emergency|trapped)\b", re.I)
SAFE_RE = re.compile(
    r"(by mistake|accidentally|false alarm|pressed by accident|no emergency|not an emergency|i am (fine|ok|okay|safe)|i'?m (fine|ok|okay|safe)|everything is fine)",
    re.I)


def text_signal(text: str) -> str | None:
    """'safe' for a clear all-clear, 'help' for a clear request, else None. A heuristic, never a diagnosis."""
    if SAFE_RE.search(text):
        return "safe"
    if HELP_RE.search(text):
        return "help"
    return None


class FalseAlarmService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[FalseAlarmRecord] = Repo(ctx.store, "fa", FalseAlarmRecord)
        self.base_cfg = fam.load_config()
        s = ctx.settings
        if s.fa_hold_ms is not None:
            self.base_cfg["hold_ms"] = s.fa_hold_ms
        if s.fa_countdown_s is not None:
            self.base_cfg["countdown_s"] = s.fa_countdown_s

    # ---- config / model ---------------------------------------------------------------------------
    async def cfg(self) -> dict:
        m = await self.ctx.store.get("fa_model", "active")
        if m:
            return fam.with_model(self.base_cfg, m["weights"], m["bias"], m["version"])
        return self.base_cfg

    def client_config(self) -> dict:
        c = self.base_cfg
        return {"hold_ms": c["hold_ms"], "countdown_s": c["countdown_s"], "model_version": c["model_version"],
                "checkin": {"prompts_at_s": c["checkin"]["prompts_at_s"], "volume": c["checkin"]["volume"],
                            "unanswered_at_s": c["checkin"]["unanswered_at_s"]}}

    async def model_info(self) -> dict:
        cfg = await self.cfg()
        cands = [c for c in await self.ctx.store.list("fa_model_candidate")]
        active = await self.ctx.store.get("fa_model", "active")
        return {"active_version": cfg["model_version"], "config_hash": fam.config_hash(cfg), "weights": cfg["weights"],
                "bias": cfg["bias"], "activated_by": (active or {}).get("activated_by"), "candidates": cands}

    async def propose_model(self, body: ModelProposal, actor: str) -> dict:
        cfg = await self.cfg()
        if body.version == cfg["model_version"] or await self.ctx.store.get("fa_model_candidate", body.version):
            raise RuleViolation(f"model version {body.version} already exists; use a new version")
        unknown = set(body.weights) - set(self.base_cfg["weights"])
        if unknown:
            raise RuleViolation(f"unknown feature weight(s): {sorted(unknown)}")
        if any((not math.isfinite(v)) or abs(v) > 12 for v in [*body.weights.values(), body.bias]):
            raise RuleViolation("weights must be finite and within +-12")
        doc = {"id": body.version, "version": body.version, "weights": {**cfg["weights"], **body.weights}, "bias": body.bias,
               "status": "proposed", "proposed_by": actor, "proposed_at": self.ctx.now().isoformat(),
               "note": body.note, "metrics": body.metrics}
        await self.ctx.store.put("fa_model_candidate", body.version, doc)
        await self.ctx.audit.record(entity_type="false_alarm_model", entity_id=body.version, event_type="false_alarm.model_proposed",
                                    actor=actor, source="system", payload={"version": body.version, "metrics": body.metrics,
                                                                           "note": "not active until an admin approves"})
        return doc

    async def activate_model(self, version: str, actor: str, reason: str) -> dict:
        cand = await self.ctx.store.get("fa_model_candidate", version)
        if cand is None:
            raise KeyError(version)
        prev = (await self.cfg())["model_version"]
        await self.ctx.store.put("fa_model", "active", {"id": "active", "version": version, "weights": cand["weights"],
                                                        "bias": cand["bias"], "activated_by": actor})
        cand.update(status="active", activated_by=actor)
        await self.ctx.store.put("fa_model_candidate", version, cand)
        await self.ctx.audit.record(entity_type="false_alarm_model", entity_id=version, event_type="false_alarm.model_activated",
                                    actor=actor, source="human_confirmed", reason=reason, old={"version": prev}, new={"version": version})
        return await self.model_info()

    # ---- records ----------------------------------------------------------------------------------
    async def register(self, inc: Incident, device_id: str | None) -> FalseAlarmRecord:
        loc = inc.location_candidates[0]
        rec = FalseAlarmRecord(
            id=inc.id, created_at=self.ctx.now(),
            device_hash=fam.hash_device(device_id, self.base_cfg["history"]["salt"]),
            place_type=fam.place_type_at(loc.lat, loc.lon, self.ctx.settings.seed))
        await self.repo.put(rec)
        return rec

    async def get(self, incident_id: str) -> FalseAlarmRecord | None:
        return await self.repo.get(incident_id)

    async def _accidents(self, rec: FalseAlarmRecord) -> int:
        if not rec.device_hash:
            return 0
        d = await self.ctx.store.get("fa_device", rec.device_hash)
        return int(d["accidents"]) if d else 0

    async def _bump_device(self, rec: FalseAlarmRecord) -> None:
        if not rec.device_hash:
            return
        d = await self.ctx.store.get("fa_device", rec.device_hash) or {"id": rec.device_hash, "accidents": 0}
        d["accidents"] = int(d["accidents"]) + 1
        await self.ctx.store.put("fa_device", rec.device_hash, d)

    async def _corroboration(self, inc: Incident) -> list[str]:
        c = self.base_cfg["corroboration"]
        loc = inc.location_candidates[0]
        out = []
        now = self.ctx.now()
        for o in await self.ctx.incident_repo.list():
            if o.id == inc.id or o.status in ("cancelled_by_caller", "merged") or o.created_by == inc.created_by:
                continue
            if (now - o.created_at).total_seconds() > c["window_s"] or not o.location_candidates:
                continue
            ol = o.location_candidates[0]
            if haversine_m(loc.lat, loc.lon, ol.lat, ol.lon) <= c["radius_m"]:
                out.append(o.id)
        return out

    # ---- scoring ----------------------------------------------------------------------------------
    async def assess(self, incident_id: str, trigger: str) -> Assessment:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        rec = await self.repo.get(incident_id)
        if inc is None or rec is None:
            raise KeyError(incident_id)
        cfg = await self.cfg()
        corro = await self._corroboration(inc)
        st = rec.checkin.state
        a = fam.score(cfg, features=rec.features, mic_consent=rec.mic_consent, place_type=rec.place_type, now=ctx.now(),
                      device_accidents=await self._accidents(rec), corroborating=len(corro),
                      checkin_state=st if st in ("safe", "help", "danger", "unanswered") else "none",
                      explicit_help=rec.explicit_help, trigger=trigger)
        if rec.min_lane and LANE_RANK[rec.min_lane] > LANE_RANK[a.lane]:
            a.lane = rec.min_lane
            a.overrides.append("never_dismiss_floor")
        prev = rec.lane

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.lane, r.corroborating = a.lane, corro
            r.assessments = [*r.assessments, a][-30:]
            return r

        await self.repo.modify(incident_id, fn)
        await ctx.audit.record(
            entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.assessed", actor="false-alarm-model",
            correlation_id=incident_id, source="system",
            payload={"lane": a.lane, "base_lane": a.base_lane, "probability_genuine": a.probability_genuine, "trigger": trigger,
                     "overrides": a.overrides, "missing": a.missing, "model_version": a.model_version,
                     "config_hash": a.config_hash, "previous_lane": prev,
                     "top": [{"feature": c.feature, "contribution": c.contribution} for c in fam.top_signals(a)]})
        return a

    # ---- layer 1: pending -> live / cancelled -------------------------------------------------------
    async def go_live(self, incident_id: str, why: str, *, actor: str = "system", floor: str | None = None) -> None:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        rec = await self.repo.get(incident_id)
        if inc is None or rec is None or inc.status in INACTIVE or rec.live_at is not None:
            return
        now = ctx.now()
        was_pending = inc.status == "pending"

        def mk(i: Incident) -> Incident:
            if i.status == "pending":
                i.status, i.updated_at = "new", now
            return i

        inc, _ = await ctx.incident_repo.modify(incident_id, mk)

        def live(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.live_at = r.live_at or now
            r.min_lane = floor or r.min_lane
            return r

        await self.repo.modify(incident_id, live)
        await ctx.audit.record(entity_type="incident", entity_id=incident_id, event_type="incident.went_live", actor=actor,
                               correlation_id=incident_id, source="system" if actor == "system" else "reported",
                               reason=why, old={"status": "pending" if was_pending else inc.status}, new={"status": inc.status},
                               vis=vis_for(inc))
        a = await self.assess(incident_id, why)
        if a.lane in self.base_cfg["checkin"]["lanes"]:
            await self._start_checkin(incident_id, "live")
        await self._apply_lane(incident_id, why)

    async def _start_checkin(self, incident_id: str, reason: str) -> None:
        now = self.ctx.now()

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord | None:
            if r.checkin.state != "none":
                return None
            r.checkin = CheckIn(state="prompting", started_at=now, reason=reason)
            return r

        await self.repo.modify(incident_id, fn)
        await self.advance(incident_id)

    async def _apply_lane(self, incident_id: str, why: str, *, actor: str = "system") -> None:
        """HIGH / UNCERTAIN dispatch now. LOW waits for the verification window, never longer."""
        ctx = self.ctx
        rec = await self.repo.get(incident_id)
        inc = await ctx.incident_repo.get(incident_id)
        if rec is None or inc is None or inc.status in (*INACTIVE, "pending") or rec.dispatch_started:
            return
        if not await self.auto_dispatch_allowed(incident_id):
            return  # the caller said they are safe: nothing is sent automatically (a dispatcher can still dispatch)
        if rec.lane in ("HIGH", "UNCERTAIN"):
            await self._start_dispatch(incident_id, why, actor)
            return
        if rec.hold_until is None and rec.checkin.state != "safe":
            until = ctx.now() + timedelta(seconds=float(self.base_cfg["low_hold_s"]))

            def hold(r: FalseAlarmRecord) -> FalseAlarmRecord:
                r.hold_until = until
                return r

            await self.repo.modify(incident_id, hold)
            await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.hold_started",
                                   actor="false-alarm-model", correlation_id=incident_id, source="system",
                                   payload={"until": until.isoformat(), "note": "LOW lane: verifying; dispatch is delayed by this window at most"})

    async def _start_dispatch(self, incident_id: str, why: str, actor: str) -> None:
        started = {"v": False}

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord | None:
            if r.dispatch_started:
                return None
            r.dispatch_started, r.hold_until = True, None
            started["v"] = True
            return r

        await self.repo.modify(incident_id, fn)
        if not started["v"]:
            return
        inc = await self.ctx.incident_repo.get(incident_id)
        await self.ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.dispatch_started",
                                    actor=actor, correlation_id=incident_id, source="system", reason=why, vis=vis_for(inc))
        self.ctx.pipeline.start(incident_id)

    async def start_pending(self, body, p: Principal) -> Incident:
        from app.schemas.call import SosRequest

        sreq = SosRequest(lat=body.lat, lon=body.lon, accuracy_m=body.accuracy_m, location_age_s=body.location_age_s,
                          approximate=body.approximate, location_source=body.location_source)
        return await self.ctx.calls.create_incident(sreq, p, pending=True, device_id=body.device_id)

    async def confirm(self, incident_id: str, p: Principal) -> tuple[Incident, CallSession]:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if inc.status == "cancelled_by_caller":
            raise RuleViolation("this request was cancelled; press SOS again to start a new one")
        if inc.status == "pending":
            call = await ctx.calls.open_call(inc, p)
            await self.go_live(incident_id, "countdown_complete", actor=p.sub)
            inc = await ctx.incident_repo.get(incident_id)
            return inc, call
        return inc, await self._call_for(incident_id, p)  # already live (confirmed twice, or server fail-safe)

    async def _call_for(self, incident_id: str, p: Principal) -> CallSession:
        for c in await self.ctx.calls.repo.list():
            if c.incident_id == incident_id:
                return c
        return await self.ctx.calls.open_call(await self.ctx.incident_repo.get(incident_id), p)

    async def cancel(self, incident_id: str, p: Principal) -> Incident:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if inc.status != "pending":
            raise RuleViolation(f"the countdown is over (status {inc.status}); a cancelled-by-mistake request is now handled by check-in")
        now = ctx.now()

        def fn(i: Incident) -> Incident | None:
            if i.status != "pending":
                return None
            i.status, i.updated_at = "cancelled_by_caller", now
            return i

        inc, outcome = await ctx.incident_repo.modify(incident_id, fn)
        if outcome != "applied":
            raise RuleViolation("the countdown just ended; the request is live")

        def mark(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.cancelled_at = now
            return r

        rec, _ = await self.repo.modify(incident_id, mark)
        await self._bump_device(rec)
        await ctx.audit.record(entity_type="incident", entity_id=incident_id, event_type="incident.cancelled_by_caller", actor=p.sub,
                               correlation_id=incident_id, source="reported", old={"status": "pending"}, new={"status": inc.status},
                               reason="caller cancelled during the countdown; record kept, one check-in queued", vis=vis_for(inc))
        await self._start_checkin(incident_id, "cancelled")
        return inc

    # ---- layer 2: signals ---------------------------------------------------------------------------
    async def ingest(self, incident_id: str, batch: SignalBatch) -> Assessment:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        rec = await self.repo.get(incident_id)
        if inc is None or rec is None:
            raise KeyError(incident_id)
        if inc.status in INACTIVE:
            raise RuleViolation(f"incident is {inc.status}")
        bad = [f.name for f in batch.features if f.name not in DEVICE_FEATURES]
        if bad:
            raise RuleViolation(f"unknown signal(s): {bad}")
        now = ctx.now()

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.mic_consent = batch.mic_consent
            feats = dict(r.features)
            for f in batch.features:
                feats[f.name] = f.model_copy(update={"at": f.at or now})
            if not batch.mic_consent:
                for k in fam.MIC_FEATURES:
                    feats.pop(k, None)  # consent withdrawn: forget the numbers
            r.features = feats
            if batch.device_id and not r.device_hash:
                r.device_hash = fam.hash_device(batch.device_id, self.base_cfg["history"]["salt"])
            return r

        await self.repo.modify(incident_id, fn)
        await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.signals_received",
                               actor=inc.created_by or "device", correlation_id=incident_id, source="measured",
                               payload={"features": {f.name: f.status for f in batch.features}, "mic_consent": batch.mic_consent,
                                        "note": "derived numbers only; no audio"}, vis=vis_for(inc))
        a = await self.assess(incident_id, "signals")
        await self._apply_lane(incident_id, "signals")
        return a

    async def on_caller_text(self, incident_id: str, text: str) -> None:
        sig = text_signal(text)
        rec = await self.repo.get(incident_id)
        if sig is None or rec is None:
            return
        if sig == "help":
            def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
                r.explicit_help = True
                return r

            await self.repo.modify(incident_id, fn)
            await self.assess(incident_id, "explicit_help")
            await self._apply_lane(incident_id, "explicit_help")
        elif rec.checkin.state in ("prompting", "unanswered"):
            await self.respond(incident_id, "safe", via="caller words")

    # ---- layer 4: check-in --------------------------------------------------------------------------
    async def advance(self, incident_id: str) -> None:
        """Move the ladder forward (prompts at T+0/15/30, unanswered at T+45). Never dismisses."""
        ctx = self.ctx
        rec = await self.repo.get(incident_id)
        if rec is None or rec.checkin.state != "prompting" or rec.checkin.started_at is None:
            return
        cfg = self.base_cfg["checkin"]
        now = ctx.now()
        elapsed = (now - rec.checkin.started_at).total_seconds()
        new_prompts: list[Prompt] = []
        went_unanswered = {"v": False}

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord | None:
            if r.checkin.state != "prompting":
                return None
            changed = False
            for idx, t in enumerate(cfg["prompts_at_s"]):
                # HIGH requests are not interrupted, but a caller who cancelled is always asked
                if elapsed >= t and len(r.checkin.prompts) < idx + 1 and (r.lane != "HIGH" or r.checkin.reason == "cancelled"):
                    pr = Prompt(n=idx + 1, at=now, volume=cfg["volume"][min(idx, len(cfg["volume"]) - 1)])
                    r.checkin.prompts = [*r.checkin.prompts, pr]
                    new_prompts.append(pr)
                    changed = True
            if elapsed >= cfg["unanswered_at_s"] and r.checkin.prompts:
                r.checkin.state = "unanswered"
                went_unanswered["v"] = True
                changed = True
            return r if changed else None

        await self.repo.modify(incident_id, fn)
        inc = await ctx.incident_repo.get(incident_id)
        for pr in new_prompts:
            await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.checkin_prompt",
                                   actor="check-in", correlation_id=incident_id, source="system",
                                   payload={"n": pr.n, "volume": pr.volume, "text": "Are you OK? Tap if safe"}, vis=vis_for(inc))
        if went_unanswered["v"]:
            await self._on_unanswered(incident_id)

    async def _on_unanswered(self, incident_id: str) -> None:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.checkin_unanswered",
                               actor="check-in", correlation_id=incident_id, source="system",
                               payload={"note": "no answer: escalated, never dismissed"}, vis=vis_for(inc))
        if inc.status == "cancelled_by_caller":
            def alert(r: FalseAlarmRecord) -> FalseAlarmRecord:
                r.followup_alert = True
                return r

            await self.repo.modify(incident_id, alert)
            return
        await self.assess(incident_id, "unanswered")
        await self._apply_lane(incident_id, "unanswered_checkin")

    async def respond(self, incident_id: str, response: str, *, via: str = "tap", p: Principal | None = None) -> dict:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        rec = await self.repo.get(incident_id)
        if inc is None or rec is None:
            raise KeyError(incident_id)
        if rec.checkin.state not in ("prompting", "unanswered"):
            raise RuleViolation("there is no open check-in")
        now = ctx.now()

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.checkin.state, r.checkin.response, r.checkin.responded_at = response, response, now
            r.followup_alert = False
            if response == "safe":
                r.hold_until = None  # stop waiting; the incident stays open for a dispatcher to close with a reason
            return r

        await self.repo.modify(incident_id, fn)
        await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.checkin_response",
                               actor=(p.sub if p else inc.created_by or "caller"), correlation_id=incident_id, source="reported",
                               payload={"response": response, "via": via,
                                        "note": "safe lowers the score but does not close the incident" if response == "safe" else "escalated"},
                               vis=vis_for(inc))
        if response in ("help", "danger") and inc.status == "cancelled_by_caller":
            await self._reopen(incident_id, p)
        a = await self.assess(incident_id, "checkin_response")
        dispatch = "not_started"
        if response != "safe":
            await self._apply_lane(incident_id, f"checkin_{response}")
            dispatch = "continues"
        else:
            dispatch = await self._stop_for_safe(incident_id, p.sub if p else (inc.created_by or "caller"))
        return {"state": response, "lane": a.lane, "dispatch": dispatch}

    # ---- "I am safe": stop the automatic dispatch --------------------------------------------------------
    async def auto_dispatch_allowed(self, incident_id: str) -> bool:
        """False once the caller has said they are safe, unless the situation is HIGH (a crash signature, a call for help or a strong
        score) or a dispatcher chose to dispatch. Nothing is dismissed: the incident stays open for a dispatcher."""
        rec = await self.repo.get(incident_id)
        if rec is None or rec.dispatcher_override:
            return True
        return not (rec.checkin.state == "safe" and rec.lane != "HIGH")

    async def _stop_for_safe(self, incident_id: str, actor: str) -> str:
        rec = await self.repo.get(incident_id)
        inc = await self.ctx.incident_repo.get(incident_id)
        if rec is None or inc is None or inc.status in (*INACTIVE, "pending"):
            return "not_started"
        if not await self.auto_dispatch_allowed(incident_id):
            if rec.dispatch_started:
                return await self.recall_dispatch(incident_id, "caller said they are safe", actor)
            return "not_started"
        return "continues"  # HIGH: help is still sent and the caller is told why

    async def recall_dispatch(self, incident_id: str, reason: str, actor: str) -> str:
        """Stop an automatic assignment that has not reached the patient: cancel the assignment and any pending recommendation,
        supersede the hospital pre-alert, release the unit, and leave the incident open for a dispatcher. Everything is audited."""
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        rec = await self.repo.get(incident_id)
        if inc is None or rec is None:
            return "not_started"
        vid = inc.assigned_vehicle_id
        v = await ctx.vehicle_repo.get(vid) if vid else None
        if v is not None and v.status in ("on_scene", "transporting"):
            return "unit_on_scene"  # a crew with the patient is never recalled automatically
        now = ctx.now()
        for d in await ctx.dispatch.repo.list():
            if d.incident_id == incident_id and d.status == "pending":
                d.status = "superseded"
                await ctx.dispatch.repo.put(d)
                await ctx.audit.record(entity_type="dispatch", entity_id=d.id, event_type="dispatch.superseded", actor=actor,
                                       correlation_id=incident_id, source="system", reason=reason, vis=vis_for(inc))
        for a in await ctx.acks.list(incident_id):
            if a.state not in ("rejected", "cancelled"):
                await ctx.acks.cancel(a.id, actor, reason)
        if v is not None and v.status != "available":
            await ctx.vehicles.apply_update(v.id, VehicleStateUpdate(
                update_id=f"{incident_id}-recall-{v.id}", occurred_at=now, status="available",
                reason=f"assignment recalled: {reason}", source="reported"), actor)
        if vid:
            ctx.nav.reset(vid)
            ctx.demo_driver.cancel(vid)
        pa = await ctx.cases.for_incident(incident_id)
        if pa is not None and pa.status not in ("arrived", "handover_started", "handover_completed"):
            pa.status, pa.superseded_reason, pa.updated_at = "superseded", f"assignment recalled: {reason}", now
            await ctx.cases.repo.put(pa)
            await ctx.audit.record(entity_type="prealert", entity_id=pa.id, event_type="prealert.superseded", actor=actor,
                                   correlation_id=incident_id, source="system", reason=pa.superseded_reason,
                                   vis={"hospital_id": pa.hospital_id, "caller": inc.created_by})

        def clear(i: Incident) -> Incident:
            i.assigned_vehicle_id, i.destination_id, i.status, i.updated_at = None, None, "new", now
            return i

        inc, _ = await ctx.incident_repo.modify(incident_id, clear)

        def mark(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.dispatch_started, r.hold_until, r.recalled_at = False, None, now
            return r

        await self.repo.modify(incident_id, mark)
        await ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.dispatch_recalled", actor=actor,
                               correlation_id=incident_id, source="reported", reason=reason, vis=vis_for(inc),
                               payload={"vehicle_id": vid, "note": "automatic assignment stopped; the incident stays open for a dispatcher to close or dispatch"})
        return "stopped"

    async def _reopen(self, incident_id: str, p: Principal | None) -> None:
        ctx = self.ctx
        now = ctx.now()

        def fn(i: Incident) -> Incident:
            i.status, i.updated_at = "new", now
            return i

        inc, _ = await ctx.incident_repo.modify(incident_id, fn)
        await ctx.audit.record(entity_type="incident", entity_id=incident_id, event_type="incident.reopened_by_checkin",
                               actor=p.sub if p else "caller", correlation_id=incident_id, source="reported",
                               old={"status": "cancelled_by_caller"}, new={"status": "new"},
                               reason="caller asked for help after cancelling", vis=vis_for(inc))
        await ctx.calls.open_call(inc, p or Principal(inc.created_by or "caller", "caller", "caller"))

        def live(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.live_at, r.min_lane, r.cancelled_at = now, "HIGH", r.cancelled_at
            return r

        await self.repo.modify(incident_id, live)

    # ---- timers ------------------------------------------------------------------------------------
    async def tick(self) -> None:
        ctx = self.ctx
        cfg = self.base_cfg
        now = ctx.now()
        for rec in await self.repo.list():
            if rec.close_reason or rec.tag:
                continue
            inc = await ctx.incident_repo.get(rec.id)
            if inc is None:
                continue
            if inc.status == "pending":
                if (now - inc.created_at).total_seconds() > cfg["countdown_s"] + cfg["pending_grace_s"]:
                    # the app vanished mid-countdown: fail safe, make it live (never silently drop a possible emergency)
                    owner = Principal(inc.created_by or "caller", "caller", "caller")
                    await ctx.calls.open_call(inc, owner)
                    await self.go_live(rec.id, "countdown_elapsed_no_client", floor="UNCERTAIN")
                continue
            if inc.status in ("closed", "merged"):
                continue
            await self.advance(rec.id)
            rec = await self.repo.get(rec.id)
            if rec and rec.hold_until and not rec.dispatch_started and now >= rec.hold_until and inc.status not in INACTIVE:
                def floor(r: FalseAlarmRecord) -> FalseAlarmRecord:
                    r.min_lane = "UNCERTAIN"
                    return r

                await self.repo.modify(rec.id, floor)
                await self.assess(rec.id, "hold_expired")
                await self._apply_lane(rec.id, "hold_expired")

    # ---- dispatcher actions ---------------------------------------------------------------------------
    async def dispatch_now(self, incident_id: str, actor: str) -> None:
        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.min_lane, r.dispatcher_override = "UNCERTAIN", True
            return r

        await self.repo.modify(incident_id, fn)
        await self.assess(incident_id, "dispatcher_override")
        await self._start_dispatch(incident_id, "dispatcher chose to dispatch now", actor)

    async def tag(self, incident_id: str, outcome: str, reason: str, actor: str) -> FalseAlarmRecord:
        now = self.ctx.now()

        def fn(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.tag = OutcomeTag(outcome=outcome, reason=reason, by=actor, at=now)  # type: ignore[arg-type]
            return r

        rec, _ = await self.repo.modify(incident_id, fn)
        if outcome == "accidental":
            await self._bump_device(rec)
        inc = await self.ctx.incident_repo.get(incident_id)
        await self.ctx.audit.record(entity_type="false_alarm", entity_id=incident_id, event_type="false_alarm.outcome_tagged",
                                    actor=actor, correlation_id=incident_id, source="human_confirmed", reason=reason,
                                    payload={"outcome": outcome, "lane": rec.lane, "model_version": rec.assessments[-1].model_version
                                             if rec.assessments else None}, vis=vis_for(inc))
        return rec

    async def close(self, incident_id: str, reason: str, actor: str) -> Incident:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if inc.status in ("closed", "merged"):
            raise RuleViolation(f"incident is {inc.status}")
        v = await ctx.vehicle_repo.get(inc.assigned_vehicle_id) if inc.assigned_vehicle_id else None
        if v and v.status in ("on_scene", "transporting"):
            raise RuleViolation(f"{v.id} is {v.status.replace('_', ' ')}: complete the handover instead of closing")
        now = ctx.now()
        prev = inc.status

        def fn(i: Incident) -> Incident:
            i.status, i.updated_at = "closed", now
            return i

        inc, _ = await ctx.incident_repo.modify(incident_id, fn)

        def mark(r: FalseAlarmRecord) -> FalseAlarmRecord:
            r.close_reason = reason
            return r

        await self.repo.modify(incident_id, mark)
        await ctx.audit.record(entity_type="incident", entity_id=incident_id, event_type="incident.closed_by_dispatcher", actor=actor,
                               correlation_id=incident_id, source="human_confirmed", reason=reason, old={"status": prev},
                               new={"status": "closed"}, vis=vis_for(inc))
        if v and v.status in ("assigned", "accepted", "en_route"):
            await ctx.vehicles.apply_update(v.id, VehicleStateUpdate(
                update_id=f"{incident_id}-close-release", occurred_at=now, status="available",
                reason=f"incident closed by dispatcher: {reason}", source="reported"), actor)
        return inc

    # ---- views ---------------------------------------------------------------------------------------
    def _view(self, rec: FalseAlarmRecord, inc: Incident) -> dict:
        now = self.ctx.now()
        a = rec.assessments[-1] if rec.assessments else None
        cfg = self.base_cfg["checkin"]
        ci = rec.checkin
        last = ci.prompts[-1] if ci.prompts else None
        nxt = None
        if ci.state == "prompting" and ci.started_at:
            el = (now - ci.started_at).total_seconds()
            ahead = [t for t in [*cfg["prompts_at_s"], cfg["unanswered_at_s"]] if t > el]
            nxt = round(min(ahead) - el, 1) if ahead else None
        return {
            "incident_id": rec.id, "status": inc.status, "created_at": inc.created_at, "live_at": rec.live_at,
            "lane": rec.lane, "probability_genuine": a.probability_genuine if a else None,
            "base_lane": a.base_lane if a else None, "model_version": a.model_version if a else None,
            "config_hash": a.config_hash if a else None, "bias": a.bias if a else None, "logit": a.logit if a else None,
            "contributions": [c.model_dump() for c in a.contributions] if a else [],
            "top": [c.model_dump() for c in fam.top_signals(a)] if a else [], "missing": a.missing if a else [],
            "overrides": a.overrides if a else [], "place_type": rec.place_type, "corroborating": rec.corroborating,
            "mic_consent": rec.mic_consent,
            "checkin": {"state": ci.state, "reason": ci.reason, "prompt_n": last.n if last else 0,
                        "volume": last.volume if last else 0, "last_prompt_at": last.at if last else None,
                        "next_in_s": nxt, "response": ci.response},
            "hold_remaining_s": None if not rec.hold_until or rec.dispatch_started else max(0.0, round((rec.hold_until - now).total_seconds(), 1)),
            "dispatch_started": rec.dispatch_started, "recalled": rec.recalled_at is not None,
            "awaiting_review": rec.lane == "LOW" and ci.state == "safe" and not rec.dispatch_started and inc.status not in INACTIVE,
            "followup_alert": rec.followup_alert, "tag": rec.tag, "close_reason": rec.close_reason,
        }

    async def view(self, incident_id: str) -> dict | None:
        rec = await self.repo.get(incident_id)
        inc = await self.ctx.incident_repo.get(incident_id)
        if rec is None or inc is None:
            return None
        return self._view(rec, inc)

    async def list_views(self) -> list[dict]:
        incs = {i.id: i for i in await self.ctx.incident_repo.list()}
        return [self._view(r, incs[r.id]) for r in await self.repo.list() if r.id in incs]

    async def caller_view(self, incident_id: str) -> dict | None:
        v = await self.view(incident_id)
        if v is None:
            return None
        ci = v["checkin"]
        stopped = v["recalled"] or (ci["state"] == "safe" and not v["dispatch_started"] and v["lane"] != "HIGH")
        return {"incident_id": incident_id, "status": v["status"], "checkin": ci, "dispatch_stopped": stopped,
                "prompt": ci["state"] == "prompting" and ci["prompt_n"] > 0,
                "message": "Are you OK? Tap if safe", "countdown_s": self.base_cfg["countdown_s"]}


def feature_dict(features: dict[str, Feature]) -> dict[str, dict]:
    return {k: v.model_dump(mode="json") for k, v in features.items()}


__all__ = ["FalseAlarmService", "text_signal", "feature_dict"]
