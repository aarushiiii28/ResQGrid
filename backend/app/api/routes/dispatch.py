from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_ctx
from app.api.signing import signed
from app.core.security import Principal, require
from app.schemas.dispatch import ApproveRequest, BatchRequest, DispatchDecision, RecommendRequest
from app.services.context import Ctx

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])
OPS = ("dispatcher", "admin")


@router.post("/recommend", response_model=DispatchDecision)
async def recommend(body: RecommendRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Rank vehicle/destination pairs. Does NOT commit an assignment."""
    dec = await ctx.dispatch.recommend(body.incident_id, policy=body.policy, override=body.weights, actor=p.sub)
    if body.autopilot:
        ctx.autopilot.schedule(dec.id)
    return dec


@router.get("/decisions", response_model=list[DispatchDecision])
async def decisions(incident_id: str | None = None, p: Principal = Depends(require(*OPS)),
                    ctx: Ctx = Depends(get_ctx)):
    items = await ctx.dispatch.repo.list()
    return [d for d in items if incident_id is None or d.incident_id == incident_id]


@router.get("/decisions/{decision_id}", response_model=DispatchDecision)
async def decision(decision_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    d = await ctx.dispatch.repo.get(decision_id)
    if d is None:
        raise HTTPException(404, "decision not found")
    return d


@router.post("/batch")
async def batch(body: BatchRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Multi-incident assignment: greedy versus OR-Tools on identical costs (does not commit)."""
    return await ctx.dispatch.batch_compare(body.incident_ids)


class AutopilotBody(BaseModel):
    enabled: bool


@router.get("/autopilot")
async def autopilot_status(p: Principal = Depends(require("dispatcher", "admin", "caller")), ctx: Ctx = Depends(get_ctx)):
    return ctx.autopilot.status()


@router.post("/autopilot")
async def autopilot_set(body: AutopilotBody, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    old = ctx.autopilot.enabled
    ctx.autopilot.enabled = body.enabled
    await ctx.audit.record(entity_type="system", entity_id="demo-autopilot", event_type="autopilot.toggled",
                           actor=p.sub, old=old, new=body.enabled)
    return ctx.autopilot.status()


@router.get("/hospital-match")
async def hospital_match(category: str, lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                         p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.dispatch.hospital_match(category, (lat, lon))


@router.post("/{decision_id}/approve", response_model=DispatchDecision)
async def approve(decision_id: str, body: ApproveRequest, p: Principal = Depends(require(*OPS)),
                  ctx: Ctx = Depends(get_ctx), _sig=Depends(signed("dispatch.approve", "dispatch.*"))):
    """Approve, reject or override (with reason). Hard constraints cannot be overridden."""
    return await ctx.dispatch.approve(decision_id, body, p.sub)
