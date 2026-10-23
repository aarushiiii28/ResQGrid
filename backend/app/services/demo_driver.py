"""Simulated crew. When no human driver app is connected for a vehicle, a scripted crew delivers,
acknowledges, accepts, drives and reports status so the whole flow can run hands-free. Everything it
does is audited with actor `simulated-driver` and labelled simulated. Hospital handover stays a human
action in the hospital console."""

import asyncio

from app.schemas.vehicle import VehicleStateUpdate
from app.services.incident_service import RuleViolation

ACTOR = "simulated-driver"


class DemoDriverService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks: dict[str, asyncio.Task] = {}

    def driver_connected(self, vehicle_id: str) -> bool:
        return any(c.principal.role == "driver" and c.principal.vehicle_id == vehicle_id for c in self.ctx.hub.clients)

    def start(self, assignment_id: str) -> asyncio.Task | None:
        s = self.ctx.settings
        if not s.sim_driver:
            return None
        t = self.ctx.spawn(self._run(assignment_id))
        return t

    def cancel(self, vehicle_id: str) -> None:
        t = self.tasks.pop(vehicle_id, None)
        if t and not t.done():
            t.cancel()

    async def _still_mine(self, a_id: str) -> bool:
        a = await self.ctx.acks.repo.get(a_id)
        if a is None or a.state == "rejected":
            return False
        inc = await self.ctx.incident_repo.get(a.incident_id)
        return bool(inc and inc.assigned_vehicle_id == a.vehicle_id and inc.status not in ("closed", "merged", "cancelled_by_caller"))

    async def _status(self, vid: str, status: str, key: str, reason: str) -> None:
        await self.ctx.vehicles.apply_update(vid, VehicleStateUpdate(
            update_id=f"sim-{vid}-{key}-{self.ctx.now().timestamp():.3f}", occurred_at=self.ctx.now(), status=status,  # type: ignore[arg-type]
            reason=reason, source="simulated"), ACTOR)

    async def _run(self, a_id: str) -> None:
        ctx, s = self.ctx, self.ctx.settings
        a = await ctx.acks.repo.get(a_id)
        if a is None:
            return
        vid = a.vehicle_id
        self.tasks[vid] = asyncio.current_task()  # type: ignore[assignment]
        try:
            await asyncio.sleep(s.sim_driver_delay_s)
            if self.driver_connected(vid) or not await self._still_mine(a_id):
                return  # a real driver app is open: the human decides
            await ctx.acks.delivered(a_id, ACTOR)
            await asyncio.sleep(s.sim_driver_delay_s / 2)
            await ctx.acks.acknowledge(a_id, ACTOR)
            await asyncio.sleep(s.sim_driver_delay_s / 2)
            if not await self._still_mine(a_id):
                return
            await ctx.acks.accept(a_id, ACTOR)
            await asyncio.sleep(s.sim_driver_delay_s / 2)
            await self._status(vid, "en_route", "enroute", "simulated crew departed")
            ctx.nav.reset(vid)
            if not await ctx.nav.drive(vid) or not await self._still_mine(a_id):
                return
            await self._status(vid, "on_scene", "onscene", "simulated crew at scene")
            await asyncio.sleep(s.sim_on_scene_hold_s)
            await self._status(vid, "transporting", "transport", "simulated crew transporting")
            ctx.nav.reset(vid)
            if not await ctx.nav.drive(vid) or not await self._still_mine(a_id):
                return
            await ctx.driver_events.arrived(vid, ACTOR, source="simulated")
        except (RuleViolation, KeyError):
            return
        finally:
            if self.tasks.get(vid) is asyncio.current_task():
                self.tasks.pop(vid, None)
