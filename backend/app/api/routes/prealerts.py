from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.api.signing import signed
from app.core.security import Principal, require
from app.schemas.hospital_case import ChecklistUpdate, PreAlert
from app.services.context import Ctx

router = APIRouter(prefix="/api/prealerts", tags=["hospital"])
READ = ("hospital", "dispatcher", "admin")


async def _visible(ctx: Ctx, pid: str, p: Principal) -> PreAlert:
    pa = await ctx.cases.repo.get(pid)
    if pa is None:
        raise HTTPException(404, "pre-alert not found")
    if p.role == "hospital" and pa.hospital_id != p.hospital_id:
        raise HTTPException(403, "pre-alert belongs to another hospital")
    return pa


@router.get("", response_model=list[PreAlert])
async def list_prealerts(p: Principal = Depends(require(*READ)), ctx: Ctx = Depends(get_ctx)):
    items = await ctx.cases.repo.list()
    if p.role == "hospital":
        items = [x for x in items if x.hospital_id == p.hospital_id]
    return sorted(items, key=lambda x: x.created_at, reverse=True)


@router.get("/{pid}", response_model=PreAlert)
async def get_prealert(pid: str, p: Principal = Depends(require(*READ)), ctx: Ctx = Depends(get_ctx)):
    return await _visible(ctx, pid, p)


@router.post("/{pid}/checklist", response_model=PreAlert)
async def checklist(pid: str, body: ChecklistUpdate, p: Principal = Depends(require("hospital", "admin")),
                    ctx: Ctx = Depends(get_ctx)):
    await _visible(ctx, pid, p)
    return await ctx.cases.update_checklist(pid, body, p.sub)


@router.post("/{pid}/{action}", response_model=PreAlert)
async def act(pid: str, action: Literal["acknowledge", "arrival", "handover_started", "handover_completed"],
              p: Principal = Depends(require("hospital", "admin")), ctx: Ctx = Depends(get_ctx),
              _sig=Depends(signed("prealert.{action}", "prealert.*"))):
    await _visible(ctx, pid, p)
    c = ctx.cases
    if action == "acknowledge":
        return await c.acknowledge(pid, p.sub)
    if action == "arrival":
        return await c.arrival(pid, p.sub, source="hospital")
    if action == "handover_started":
        return await c.handover_started(pid, p.sub)
    return await c.handover_completed(pid, p.sub)
