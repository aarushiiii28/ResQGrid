"""M16 in-app simulated call. Operator lines are fixed scripts (no LLM voice), at most three short
questions, each asked only if the answer would change the response. No audio is stored."""

from app.core.security import Principal
from app.db.repos import Repo
from app.db.seed import localize_fleet
from app.schemas.call import (
    CallSession,
    ExtractionResult,
    LocationUpdate,
    SosRequest,
    TranscriptLine,
    Utterance,
    UtteranceResponse,
)
from app.schemas.common import Source
from app.schemas.incident import Incident, LocationCandidate
from app.services.incident_service import INACTIVE, RuleViolation, recompute_unknowns, vis_for

MAX_QUESTIONS = 3
OPENING = "Emergency line. Your location is shared and help is being arranged. What happened?"
CLOSING = "Help is on the way. Stay on the line. Follow the guidance on your screen."
QUESTIONS = {
    "reported_category": "What happened?",
    "conscious": "Is the person awake and breathing?",
    "patient_count": "How many people need help?",
}
# why each question matters (it changes the response), shown in the audit trail
QUESTION_WHY = {
    "reported_category": "changes unit type and destination",
    "conscious": "changes unit type and priority",
    "patient_count": "changes number of units needed",
}
URGENCY_BY_CATEGORY = {"cardiac": 1, "stroke": 1, "unconscious": 1, "breathing": 1, "neonatal": 2, "trauma": 2, "other": 3}


class CallService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[CallSession] = Repo(ctx.store, "call", CallSession)

    # ---- SOS ---------------------------------------------------------------------
    async def create_incident(self, body: SosRequest, p: Principal, *, pending: bool = False,
                              device_id: str | None = None) -> Incident:
        """Create the SOS incident. `pending` = the caller is in the countdown: not dispatched, kept forever."""
        ctx = self.ctx
        now = ctx.now()
        n = await ctx.store.next_seq("incident_seq")
        approx = body.approximate or body.location_source in ("manual_pin", "default", "last_known")
        loc = LocationCandidate(
            lat=body.lat, lon=body.lon, accuracy_m=body.accuracy_m,
            at=now, approximate=approx, source=Source.measured if not approx else Source.reported,
            label=f"{body.location_source}, {body.location_age_s:.0f}s old",
        )
        inc = Incident(id=f"INC-{n}", created_at=now, created_by=p.sub, channel="sos",
                       location_source=body.location_source, location_candidates=[loc],
                       status="pending" if pending else "new")
        recompute_unknowns(inc)
        inc.updated_at = now
        await ctx.incident_repo.put(inc)
        await ctx.audit.record(
            entity_type="incident", entity_id=inc.id, event_type="incident.pending_created" if pending else "incident.created",
            actor=p.sub, correlation_id=inc.id, source="measured" if not approx else "reported",
            new={"channel": "sos", "lat": body.lat, "lon": body.lon, "accuracy_m": body.accuracy_m,
                 "location_age_s": body.location_age_s, "approximate": approx, "location_source": body.location_source,
                 **({"status": "pending", "note": "countdown running; not dispatched"} if pending else {})},
            vis=vis_for(inc),
        )
        if await localize_fleet(ctx, body.lat, body.lon):
            await ctx.audit.record(
                entity_type="system", entity_id="fleet", event_type="fleet.localized", actor="system", source="simulated",
                correlation_id=inc.id, payload={"lat": body.lat, "lon": body.lon,
                                                "note": "synthetic fleet re-centred on the caller; simulated data"},
            )
        await ctx.false_alarm.register(inc, device_id)
        return inc

    async def open_call(self, inc: Incident, p: Principal) -> CallSession:
        ctx = self.ctx
        now = ctx.now()
        cn = await ctx.store.next_seq("call_seq")
        call = CallSession(id=f"CALL-{cn:04d}", incident_id=inc.id, started_at=now)
        call.transcript.append(TranscriptLine(speaker="operator", text=OPENING, at=now, mode="scripted"))
        call.questions_asked = 1
        call.asked_keys = ["reported_category"]
        await self.repo.put(call)
        await ctx.audit.record(
            entity_type="call", entity_id=call.id, event_type="call.started", actor=p.sub, correlation_id=inc.id,
            source="simulated", payload={"mode": call.mode, "note": "in-app simulated call; no real call placed"},
            vis=vis_for(inc),
        )
        return call

    async def sos(self, body: SosRequest, p: Principal, device_id: str | None = None) -> tuple[Incident, CallSession]:
        """Direct SOS (no countdown): used by tests, the API and the Replay Lab. The caller screen uses
        start -> countdown -> confirm instead (see FalseAlarmService)."""
        inc = await self.create_incident(body, p, device_id=device_id)
        call = await self.open_call(inc, p)
        await self.ctx.false_alarm.go_live(inc.id, "sos")
        return inc, call

    async def update_location(self, incident_id: str, body: LocationUpdate, p: Principal) -> Incident:
        ctx = self.ctx
        now = ctx.now()
        approx = body.approximate or body.location_source in ("manual_pin", "default", "last_known")
        snap: dict = {}

        def fn(i: Incident) -> Incident:
            if i.status in INACTIVE:
                raise RuleViolation(f"incident is {i.status}")
            old = i.location_candidates[0] if i.location_candidates else None
            snap["old"] = None if old is None else {"lat": old.lat, "lon": old.lon, "accuracy_m": old.accuracy_m}
            new = LocationCandidate(lat=body.lat, lon=body.lon, accuracy_m=body.accuracy_m, at=now, approximate=approx,
                                    source=Source.measured if not approx else Source.reported,
                                    label=f"{body.location_source}, {body.location_age_s:.0f}s old")
            i.location_candidates.insert(0, new)
            del i.location_candidates[5:]
            i.location_source = body.location_source
            recompute_unknowns(i)
            i.updated_at = now
            return i

        inc, _ = await ctx.incident_repo.modify(incident_id, fn)
        await ctx.audit.record(
            entity_type="incident", entity_id=incident_id, event_type="incident.location_updated", actor=p.sub,
            correlation_id=incident_id, source="measured" if not approx else "reported", old=snap["old"],
            new={"lat": body.lat, "lon": body.lon, "accuracy_m": body.accuracy_m, "approximate": approx,
                 "location_source": body.location_source}, vis=vis_for(inc),
        )
        return inc

    # ---- conversation --------------------------------------------------------------
    async def get(self, call_id: str) -> CallSession:
        c = await self.repo.get(call_id)
        if c is None:
            raise KeyError(call_id)
        return c

    def next_question(self, inc: Incident, call: CallSession) -> tuple[str, str] | None:
        """Ask only what is still unknown and would change the response; never more than three."""
        if call.questions_asked >= MAX_QUESTIONS:
            return None
        order = [("reported_category", inc.reported_category is None),
                 ("conscious", "conscious" not in inc.facts),
                 ("patient_count", inc.patient_count is None)]
        for key, unknown in order:
            if unknown and key not in call.asked_keys:
                return key, QUESTIONS[key]
        return None

    async def utterance(self, call_id: str, body: Utterance, p: Principal) -> UtteranceResponse:
        ctx = self.ctx
        call = await self.get(call_id)
        if call.ended_at:
            raise RuleViolation("call has ended")
        now = ctx.now()
        call.transcript.append(TranscriptLine(speaker="caller", text=body.text, at=now, mode=body.mode))
        inc = await ctx.incident_repo.get(call.incident_id)
        caller_text = " ".join(t.text for t in call.transcript if t.speaker == "caller")
        ex = await ctx.extraction.extract(caller_text)
        ex = await self.apply_extraction(inc, ex, caller_text)
        inc = await ctx.incident_repo.get(call.incident_id)
        q = self.next_question(inc, call)
        operator = None
        if q:
            key, operator = q
            call.questions_asked += 1
            call.asked_keys.append(key)
            await ctx.audit.record(
                entity_type="call", entity_id=call.id, event_type="call.question_asked", actor="scripted-operator",
                correlation_id=inc.id, source="simulated", payload={"key": key, "why": QUESTION_WHY[key], "n": call.questions_asked},
                vis=vis_for(inc))
        else:
            operator = CLOSING
        call.transcript.append(TranscriptLine(speaker="operator", text=operator, at=ctx.now(), mode="scripted"))
        await self.repo.put(call)
        await ctx.audit.record(
            entity_type="call", entity_id=call.id, event_type="call.caller_said", actor=p.sub, correlation_id=inc.id,
            source="reported", payload={"mode": body.mode, "chars": len(body.text)}, vis=vis_for(inc))
        await ctx.false_alarm.on_caller_text(inc.id, body.text)
        if ex.applied:
            ctx.pipeline.on_extraction(inc.id, ex.applied)
        return UtteranceResponse(transcript=call.transcript, operator=operator, questions_asked=call.questions_asked,
                                 extraction=ex, finished=q is None)

    async def apply_extraction(self, inc: Incident, ex: ExtractionResult, caller_text: str) -> ExtractionResult:
        """Write extracted fields as ai_extracted facts (with evidence). Never sets dispatch,
        never lowers urgency; conflicts with higher-ranked values are flagged for human review."""
        ctx = self.ctx
        applied, reasons = [], list(ex.review_reasons)
        out = ex.output
        pairs = [("reported_category", out.reported_category), ("conscious", out.conscious),
                 ("breathing", out.breathing), ("patient_count", out.patient_count)]
        for key, v in pairs:
            if v is None:
                continue
            cur = await ctx.incident_repo.get(inc.id)
            before = getattr(cur, key, None) if key in ("reported_category", "patient_count") else cur.facts.get(key)
            if before is not None and before.value == v.value:
                continue
            after = await ctx.incidents.set_fact(
                inc.id, key, v.value, source=Source.ai_extracted, confidence=v.confidence,
                actor="extractor", evidence=v.evidence, reason="extracted from caller words")
            now_val = getattr(after, key, None) if key in ("reported_category", "patient_count") else after.facts.get(key)
            if now_val is not None and now_val.value == v.value:
                applied.append(key)
            else:
                reasons.append(f"conflict:{key}")
        cat = out.reported_category.value if out.reported_category else None
        if cat in URGENCY_BY_CATEGORY:
            try:  # a suggested urgency; the service refuses to ever lower an existing one
                await ctx.incidents.set_fact(inc.id, "operator_priority", URGENCY_BY_CATEGORY[cat],
                                             source=Source.ai_extracted, confidence=out.reported_category.confidence,
                                             actor="extractor", evidence=out.reported_category.evidence,
                                             reason="suggested from reported category (never lowered)")
            except RuleViolation:
                pass
        if reasons:
            def flag(i: Incident) -> Incident:
                i.needs_review = True
                i.review_reasons = sorted(set(i.review_reasons) | set(reasons))
                return i

            fresh, _ = await ctx.incident_repo.modify(inc.id, flag)
            await ctx.audit.record(
                entity_type="incident", entity_id=inc.id, event_type="extraction.needs_review", actor="extractor",
                correlation_id=inc.id, source="ai_extracted", payload={"reasons": reasons}, vis=vis_for(fresh))
        return ex.model_copy(update={"applied": applied, "review_required": bool(reasons), "review_reasons": reasons})

    async def end(self, call_id: str, p: Principal) -> CallSession:
        call = await self.get(call_id)
        if not call.ended_at:
            call.ended_at = self.ctx.now()
            await self.repo.put(call)
            inc = await self.ctx.incident_repo.get(call.incident_id)
            await self.ctx.audit.record(
                entity_type="call", entity_id=call.id, event_type="call.ended", actor=p.sub,
                correlation_id=call.incident_id, source="simulated",
                payload={"note": "call ended; dispatch continues"}, vis=vis_for(inc))
        return call

