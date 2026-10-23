from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.schemas.hospital import HospitalView, ResourceUpdate
from app.schemas.vehicle import UpdateResult
from app.services.context import Ctx

router = APIRouter(prefix="/api/hospitals", tags=["hospitals"])


@router.get("", response_model=list[HospitalView])
async def list_hospitals(p: Principal = Depends(require("dispatcher", "admin", "hospital")),
                         ctx: Ctx = Depends(get_ctx)):
    views = await ctx.hospitals.list_views()
    if p.role == "hospital":
        views = [h for h in views if h.id == p.hospital_id]
    return views


@router.patch("/{hospital_id}/resources", response_model=UpdateResult)
async def patch_resources(hospital_id: str, body: ResourceUpdate,
                          p: Principal = Depends(require("hospital", "admin")),
                          ctx: Ctx = Depends(get_ctx)):
    if p.role == "hospital" and p.hospital_id != hospital_id:
        raise HTTPException(403, "hospital users may only update their own hospital")
    if await ctx.hospital_repo.get(hospital_id) is None:
        raise HTTPException(404, "hospital not found")
    return await ctx.hospitals.apply_resource_update(hospital_id, body, p.sub)
