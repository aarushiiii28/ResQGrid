from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.schemas.routing import (
    AccessReportCreate,
    DisruptionCreate,
    EntranceDecision,
    RoutePreviewRequest,
)
from app.services.access_service import AccessError
from app.services.context import Ctx

router = APIRouter(tags=["routing"])
OPS = ("dispatcher", "admin")


@router.post("/api/routes/preview")
async def route_preview(body: RoutePreviewRequest, p: Principal = Depends(require(*OPS, "driver")),
                        ctx: Ctx = Depends(get_ctx)):
    """Route estimate with duration range, source, confidence and turn-by-turn steps."""
    disruptions = await ctx.routing.active_disruptions()
    entrance = None
    if body.building_id:
        b = await ctx.building_repo.get(body.building_id)
        if b is None:
            raise HTTPException(404, "building not found")
        entrance = ctx.access.decide(b, disruptions.blocked_entrances, (body.origin_lat, body.origin_lon))
        t = entrance.chosen.entrance.location if entrance.chosen else b.centroid
        dest = (t.lat, t.lon)
    elif body.dest_lat is not None and body.dest_lon is not None:
        dest = (body.dest_lat, body.dest_lon)
    else:
        raise HTTPException(422, "dest_lat/dest_lon or building_id required")
    route = await ctx.routing.route((body.origin_lat, body.origin_lon), dest, disruptions)
    return {"route": route, "entrance": entrance, "destination": {"lat": dest[0], "lon": dest[1]}}


@router.get("/api/routing/disruptions")
async def list_disruptions(p: Principal = Depends(require(*OPS, "driver")), ctx: Ctx = Depends(get_ctx)):
    return (await ctx.routing.active_disruptions()).items


@router.post("/api/routing/disruptions", status_code=201)
async def add_disruption(body: DisruptionCreate, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    try:
        d = await ctx.routing.add_disruption(body, p.sub)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    await ctx.nav.check_all()  # drivers get a one-tap reroute proposal if their route is hit
    return d


class PolicyMode(BaseModel):
    enabled: bool


@router.get("/api/routing/policy")
async def get_policy(p: Principal = Depends(require(*OPS, "driver")), ctx: Ctx = Depends(get_ctx)):
    return {"enabled": ctx.stability.policy.get("enabled", True), "policy_version": ctx.stability.version,
            "mode": "stable policy: same-destination changes apply automatically" if ctx.stability.policy.get("enabled", True)
            else "human-approved: route changes are proposed by the risk monitor and applied when the driver taps"}


@router.post("/api/routing/policy")
async def set_policy(body: PolicyMode, p: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    """Admin switch between the stable-route policy (automatic same-destination changes) and the older human-approved mode."""
    before = ctx.stability.policy.get("enabled", True)
    ctx.stability.policy["enabled"] = body.enabled
    await ctx.audit.record(entity_type="routing", entity_id="route-policy", event_type="routing.policy_changed", actor=p.sub,
                           source="human_confirmed", old={"enabled": before}, new={"enabled": body.enabled},
                           payload={"policy_version": ctx.stability.version})
    return await get_policy(p, ctx)


@router.delete("/api/routing/disruptions/{disruption_id}")
async def remove_disruption(disruption_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    if not await ctx.routing.remove_disruption(disruption_id, p.sub):
        raise HTTPException(404, "disruption not found")
    await ctx.nav.check_all()
    return {"removed": disruption_id}


@router.get("/api/buildings/{building_id}/entrance", response_model=EntranceDecision)
async def entrance_decision(building_id: str, p: Principal = Depends(require(*OPS, "driver")), ctx: Ctx = Depends(get_ctx)):
    b = await ctx.building_repo.get(building_id)
    if b is None:
        raise HTTPException(404, "building not found")
    return ctx.access.decide(b, (await ctx.routing.active_disruptions()).blocked_entrances)


@router.post("/api/buildings/{building_id}/access-reports", status_code=201)
async def add_access_report(building_id: str, body: AccessReportCreate,
                            p: Principal = Depends(require(*OPS, "driver")), ctx: Ctx = Depends(get_ctx)):
    try:
        return await ctx.access.add_report(building_id, body, p.sub)
    except AccessError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/api/buildings/{building_id}/access-reports/{report_id}/reconfirm")
async def reconfirm_access_report(building_id: str, report_id: str,
                                  p: Principal = Depends(require(*OPS, "driver")), ctx: Ctx = Depends(get_ctx)):
    try:
        return await ctx.access.reconfirm(building_id, report_id, p.sub)
    except AccessError as e:
        raise HTTPException(422, str(e)) from e
