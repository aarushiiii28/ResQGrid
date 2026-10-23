"""Simulated heartbeat so demo units/hospitals do not all go stale while idle.
Labelled source=simulated. Vehicles in `ctx.extras["silenced"]` are skipped (fault injection)."""

import asyncio
import uuid
from datetime import timedelta

from app.db.seed import NEVER_CONFIRMED, STALE_DEMO
from app.schemas.vehicle import VehicleStateUpdate
from app.services.context import Ctx

HOSPITAL_REFRESH_S = 600


async def beat_once(ctx: Ctx) -> None:
    now = ctx.now()
    silenced = ctx.extras.get("silenced", set())
    for v in await ctx.vehicle_repo.list():
        if v.offline or v.id in silenced or v.location is None:
            continue
        if v.status in ("en_route", "transporting"):
            continue  # moved by the route simulation / driver, not the idle heartbeat
        await ctx.vehicles.apply_update(
            v.id,
            VehicleStateUpdate(update_id=f"hb-{uuid.uuid4().hex[:12]}", occurred_at=now,
                               location=v.location, source="simulated"),
            "simulator",
        )
    for h in await ctx.hospital_repo.list():
        if h.id in STALE_DEMO or h.id in NEVER_CONFIRMED or h.id in silenced:
            continue
        for name, r in h.resources.items():
            if r.last_confirmed_at and now - r.last_confirmed_at > timedelta(seconds=HOSPITAL_REFRESH_S):
                from app.schemas.hospital import ResourceUpdate

                await ctx.hospitals.apply_resource_update(
                    h.id,
                    ResourceUpdate(update_id=f"hb-{uuid.uuid4().hex[:12]}", occurred_at=now,
                                   resource=name, status=r.status, count=r.count, source="simulated"),
                    "simulator",
                )


async def heartbeat_loop(ctx: Ctx, every_s: float = 20.0) -> None:
    while True:
        try:
            await beat_once(ctx)
        except Exception:  # keep the loop alive; failures are visible via stale data
            pass
        await asyncio.sleep(every_s)


async def watchdog_loop(ctx: Ctx, every_s: float = 2.0) -> None:
    """M7: raise timed_out for unanswered assignments."""
    while True:
        try:
            await ctx.acks.check_timeouts()
        except Exception:
            pass
        try:
            await ctx.false_alarm.tick()  # M18: countdown fail-safe, check-in ladder, LOW-lane hold
        except Exception:
            pass
        try:
            await ctx.ledger.maybe_anchor()  # M19: Merkle anchor every N events or M minutes
        except Exception:
            pass
        await asyncio.sleep(every_s)


async def risk_loop(ctx: Ctx) -> None:
    """M8: look for trouble on live plans."""
    while True:
        try:
            await ctx.risks.evaluate()
        except Exception:
            pass
        await asyncio.sleep(ctx.settings.risk_interval_s)
