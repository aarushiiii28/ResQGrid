"""Demo Autopilot: simulates a dispatcher approving the top recommendation after a short delay.
It is labelled in the UI and every action is audited with actor `demo-autopilot`. It only
approves the recommended plan: it never overrides, rejects, or bypasses hard constraints."""

import asyncio

from app.schemas.dispatch import ApproveRequest
from app.services.incident_service import RuleViolation

ACTOR = "demo-autopilot"


class AutopilotService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.enabled: bool = ctx.settings.demo_autopilot
        self.delay_s: float = ctx.settings.demo_autopilot_delay_s
        self.tasks: set[asyncio.Task] = set()

    def status(self) -> dict:
        return {"enabled": self.enabled, "delay_s": self.delay_s, "actor": ACTOR,
                "label": "Demo Autopilot: simulated dispatcher approval"}

    def schedule(self, decision_id: str) -> asyncio.Task | None:
        if not self.enabled:
            return None
        t = asyncio.create_task(self._run(decision_id))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def _run(self, decision_id: str) -> None:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if not self.enabled:
            return
        dec = await self.ctx.dispatch.repo.get(decision_id)
        if dec is None or dec.status != "pending":
            return  # a human got there first, or the decision was superseded
        if not await self.ctx.false_alarm.auto_dispatch_allowed(dec.incident_id):
            await self.ctx.audit.record(
                entity_type="dispatch", entity_id=decision_id, event_type="dispatch.autopilot_skipped",
                actor=ACTOR, correlation_id=dec.incident_id, reason="the caller said they are safe", source="simulated")
            return
        try:
            await self.ctx.dispatch.approve(
                decision_id,
                ApproveRequest(action="approve",
                               reason=f"Demo Autopilot: simulated dispatcher approval after {self.delay_s:g}s"),
                ACTOR,
            )
        except RuleViolation as e:
            await self.ctx.audit.record(
                entity_type="dispatch", entity_id=decision_id, event_type="dispatch.autopilot_skipped",
                actor=ACTOR, correlation_id=dec.incident_id, reason=str(e), source="simulated")

    async def drain(self) -> None:
        """Wait for scheduled approvals (used by tests and the benchmark)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
