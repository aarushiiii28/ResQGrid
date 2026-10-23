"""M7 acknowledgment watchdog. created / sent / delivered_if_known / acknowledged / accepted /
rejected / timed_out are distinct. Sent is never treated as received or accepted. The channel
is a simulated in-app push; no SMS, radio or phone call exists."""

from app.core.config import Settings
from app.db.repos import Repo
from app.schemas.dispatch import AckEvent, Assignment, DispatchDecision
from app.schemas.incident import Incident
from app.schemas.vehicle import VehicleStateUpdate
from app.services.audit_service import AuditService
from app.services.incident_service import RuleViolation

LIVE_STATES = ("sent", "delivered_if_known", "acknowledged")


class AckService:
    def __init__(self, ctx):
        self.ctx = ctx
        s = ctx.store
        self.repo: Repo[Assignment] = Repo(s, "assignment", Assignment)
        self.audit: AuditService = ctx.audit
        self.settings: Settings = ctx.settings

    async def list(self, incident_id: str | None = None) -> list[Assignment]:
        items = await self.repo.list()
        return [a for a in items if incident_id is None or a.incident_id == incident_id]

    def _vis(self, a: Assignment, inc: Incident | None) -> dict:
        v = {"vehicle_id": a.vehicle_id, "hospital_id": a.hospital_id}
        if inc:
            v |= {"caller": inc.created_by}
        return v

    async def _emit(self, a: Assignment, event_type: str, actor: str, old: str | None,
                    reason: str | None = None, extra: dict | None = None) -> None:
        inc = await self.ctx.incidents.get(a.incident_id)
        await self.audit.record(
            entity_type="assignment", entity_id=a.id, event_type=event_type, actor=actor,
            correlation_id=a.incident_id, source="simulated", reason=reason, old=old, new=a.state,
            payload={"vehicle_id": a.vehicle_id, "incident_id": a.incident_id, **(extra or {})},
            vis=self._vis(a, inc),
        )

    async def create_and_send(self, inc: Incident, vehicle_id: str, hospital_id: str | None,
                              decision: DispatchDecision | None, actor: str) -> Assignment:
        now = self.ctx.now()
        n = await self.ctx.store.next_seq("assignment_seq")
        a = Assignment(
            id=f"ASG-{n:04d}", incident_id=inc.id, vehicle_id=vehicle_id, hospital_id=hospital_id,
            decision_id=decision.id if decision else None, created_at=now,
            timeout_s=self.settings.ack_timeout_s,
            history=[AckEvent(state="created", at=now, actor=actor)],
        )
        await self.repo.put(a)
        await self._emit(a, "assignment.created", actor, None)
        a.state, a.sent_at = "sent", now
        a.history.append(AckEvent(state="sent", at=now, actor="simulated-channel",
                                  note="pushed to driver app (simulated); not yet acknowledged"))
        await self.repo.put(a)
        await self._emit(a, "assignment.sent", "simulated-channel", "created")
        return a

    async def _get(self, assignment_id: str) -> Assignment:
        a = await self.repo.get(assignment_id)
        if a is None:
            raise KeyError(assignment_id)
        return a

    async def _move(self, a: Assignment, state: str, actor: str, note: str | None, event: str,
                    stamp: str, extra: dict | None = None) -> Assignment:
        old = a.state
        now = self.ctx.now()
        if old == "timed_out":
            a.late = True
        a.state = state  # type: ignore[assignment]
        setattr(a, stamp, now)
        a.history.append(AckEvent(state=state, at=now, actor=actor, note=note))  # type: ignore[arg-type]
        await self.repo.put(a)
        await self._emit(a, event, actor, old, reason=note, extra={"late": a.late, **(extra or {})})
        return a

    async def delivered(self, assignment_id: str, actor: str) -> Assignment:
        a = await self._get(assignment_id)
        if a.state != "sent":
            return a  # delivery is only meaningful right after sending; idempotent otherwise
        return await self._move(a, "delivered_if_known", actor, "driver channel connected", "assignment.delivered", "delivered_at")

    async def acknowledge(self, assignment_id: str, actor: str) -> Assignment:
        a = await self._get(assignment_id)
        if a.state in ("acknowledged", "accepted", "rejected"):
            return a
        if a.state not in ("sent", "delivered_if_known", "timed_out"):
            raise RuleViolation(f"cannot acknowledge from state {a.state}")
        return await self._move(a, "acknowledged", actor, "driver opened the assignment", "assignment.acknowledged", "acknowledged_at")

    async def accept(self, assignment_id: str, actor: str) -> Assignment:
        a = await self._get(assignment_id)
        if a.state == "accepted":
            return a
        if a.state == "rejected":
            raise RuleViolation("assignment was rejected")
        if a.acknowledged_at is None:
            a = await self.acknowledge(assignment_id, actor)
        now = self.ctx.now()
        await self.ctx.vehicles.apply_update(
            a.vehicle_id,
            VehicleStateUpdate(update_id=f"{a.id}-accept", occurred_at=now, status="accepted",
                               incident_id=a.incident_id, reason="driver accepted", source="reported"),
            actor,
        )
        return await self._move(a, "accepted", actor, "driver accepted", "assignment.accepted", "accepted_at")

    async def reject(self, assignment_id: str, actor: str, note: str | None) -> Assignment:
        a = await self._get(assignment_id)
        if a.state in ("accepted", "rejected"):
            raise RuleViolation(f"cannot reject from state {a.state}")
        now = self.ctx.now()
        await self.ctx.vehicles.apply_update(
            a.vehicle_id,
            VehicleStateUpdate(update_id=f"{a.id}-reject", occurred_at=now, status="available",
                               reason=note or "driver rejected", source="reported"),
            actor,
        )
        a = await self._move(a, "rejected", actor, note or "driver rejected", "assignment.rejected", "rejected_at")

        def clear(i: Incident) -> Incident:
            if i.assigned_vehicle_id == a.vehicle_id:
                i.assigned_vehicle_id = None
                i.status = "new"
                i.updated_at = now
            return i

        await self.ctx.incident_repo.modify(a.incident_id, clear)
        return a

    async def cancel(self, assignment_id: str, actor: str, reason: str) -> Assignment:
        """The assignment is recalled (for example the caller said they are safe). The unit is released by the caller of this method;
        nothing is deleted: the assignment stays with state `cancelled` and its reason."""
        a = await self._get(assignment_id)
        if a.state in ("rejected", "cancelled"):
            return a
        return await self._move(a, "cancelled", actor, reason, "assignment.cancelled", "cancelled_at")

    async def supersede(self, vehicle_id: str, incident_id: str, actor: str, reason: str) -> None:
        for a in await self.list(incident_id):
            if a.vehicle_id == vehicle_id and a.state not in ("rejected",):
                a.history.append(AckEvent(state=a.state, at=self.ctx.now(), actor=actor,
                                          note=f"superseded by reassignment: {reason}"))
                await self.repo.put(a)

    async def check_timeouts(self, actor: str = "watchdog") -> list[Assignment]:
        """Mark unanswered assignments timed_out and attach a simulated fallback. Does not
        reassign anything: switching units still needs human approval."""
        now = self.ctx.now()
        out = []
        for a in await self.repo.list():
            if a.state not in LIVE_STATES or a.sent_at is None:
                continue
            waited = (now - a.sent_at).total_seconds()
            if waited < a.timeout_s:
                continue
            a.fallback = await self._fallback(a, waited)
            out.append(await self._move(
                a, "timed_out", actor, f"no acceptance after {waited:.0f}s (timeout {a.timeout_s:.0f}s)",
                "assignment.timed_out", "timed_out_at", extra={"fallback": a.fallback, "alert": True},
            ))
        return out

    async def _fallback(self, a: Assignment, waited: float) -> dict:
        nxt = None
        if a.decision_id:
            dec = await self.ctx.dispatch.repo.get(a.decision_id)
            if dec:
                alt = next((c for c in dec.candidates if c.vehicle_id != a.vehicle_id and c.feasible), None)
                if alt:
                    nxt = {"vehicle_id": alt.vehicle_id, "hospital_id": alt.hospital_id}
        return {
            "kind": "simulated_escalation",
            "message": (f"No acceptance from {a.vehicle_id} after {waited:.0f}s. Simulated voice call to "
                        "crew placed (no real call). Dispatcher must approve any change of unit."),
            "next_ranked_candidate": nxt,
            "requires_human_approval": True,
        }
