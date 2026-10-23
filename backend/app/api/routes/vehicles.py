from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.schemas.vehicle import UpdateResult, VehicleStateUpdate, VehicleView
from app.services.context import Ctx

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])

DRIVER_STATUSES = {"accepted", "en_route", "on_scene", "transporting", "available"}


@router.get("", response_model=list[VehicleView])
async def list_vehicles(p: Principal = Depends(require("dispatcher", "admin", "driver")),
                        ctx: Ctx = Depends(get_ctx)):
    views = await ctx.vehicles.list_views()
    if p.role == "driver":
        views = [v for v in views if v.id == p.vehicle_id]
    return views


@router.patch("/{vehicle_id}/state", response_model=UpdateResult)
async def patch_state(vehicle_id: str, body: VehicleStateUpdate,
                      p: Principal = Depends(require("dispatcher", "admin", "driver")),
                      ctx: Ctx = Depends(get_ctx)):
    if p.role == "driver":
        if p.vehicle_id != vehicle_id:
            raise HTTPException(403, "drivers may only update their own vehicle")
        if body.status is not None and body.status not in DRIVER_STATUSES:
            raise HTTPException(403, f"drivers may not set status '{body.status}'")
    if await ctx.vehicle_repo.get(vehicle_id) is None:
        raise HTTPException(404, "vehicle not found")
    res = await ctx.vehicles.apply_update(vehicle_id, body, p.sub)
    if p.role == "driver" and body.location is not None and body.source != "simulated":
        # M20: a real device fix goes through map matching and the wrong-turn check as well
        await ctx.stability.on_fix(vehicle_id, body.location.lat, body.location.lon, body.location.accuracy_m)
        await ctx.stability.evaluate(vehicle_id)
    return res
