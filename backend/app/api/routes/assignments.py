from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.api.signing import signed
from app.core.security import Principal, require
from app.schemas.dispatch import Assignment, AssignmentAction
from app.services.context import Ctx

router = APIRouter(prefix="/api/assignments", tags=["assignments"])
OPS = ("dispatcher", "admin")


@router.get("", response_model=list[Assignment])
async def list_assignments(incident_id: str | None = None,
                           p: Principal = Depends(require(*OPS, "driver", "caller")),
                           ctx: Ctx = Depends(get_ctx)):
    items = await ctx.acks.list(incident_id)
    if p.role == "driver":
        return [a for a in items if a.vehicle_id == p.vehicle_id]
    if p.role == "caller":
        mine = {i.id for i in await ctx.incidents.list_for(p)}
        return [a for a in items if a.incident_id in mine]
    return items


@router.post("/check-timeouts", response_model=list[Assignment])
async def check_timeouts(p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.acks.check_timeouts()


@router.post("/{assignment_id}/{action}", response_model=Assignment)
async def act(assignment_id: str, action: Literal["delivered", "acknowledge", "accept", "reject"],
              body: AssignmentAction | None = None, p: Principal = Depends(require(*OPS, "driver")),
              ctx: Ctx = Depends(get_ctx), _sig=Depends(signed("assignment.{action}", "assignment.*"))):
    a = await ctx.acks.repo.get(assignment_id)
    if a is None:
        raise HTTPException(404, "assignment not found")
    if p.role == "driver" and a.vehicle_id != p.vehicle_id:
        raise HTTPException(403, "not your assignment")
    if p.role != "driver" and action in ("accept", "reject", "acknowledge"):
        raise HTTPException(403, "only the assigned driver can acknowledge, accept or reject")
    note = body.note if body else None
    if action == "delivered":
        return await ctx.acks.delivered(assignment_id, p.sub)
    if action == "acknowledge":
        return await ctx.acks.acknowledge(assignment_id, p.sub)
    if action == "accept":
        return await ctx.acks.accept(assignment_id, p.sub)
    return await ctx.acks.reject(assignment_id, p.sub, note)
