from fastapi import APIRouter, Depends

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.db.seed import seed
from app.services.context import Ctx

router = APIRouter(prefix="/api", tags=["reference"])

READERS = ("dispatcher", "admin", "caller", "driver")


@router.get("/buildings")
async def buildings(_: Principal = Depends(require(*READERS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.building_repo.list()


@router.get("/aeds")
async def aeds(_: Principal = Depends(require(*READERS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.aed_repo.list()


@router.get("/volunteers")
async def volunteers(_: Principal = Depends(require("dispatcher", "admin")),
                     ctx: Ctx = Depends(get_ctx)):
    return await ctx.volunteer_repo.list()


@router.post("/admin/reset-demo")
async def reset_demo(_: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    """Wipe all demo data (including the audit log) and reseed. Demo only."""
    ctx.reset_runtime()  # stop simulated crews and forget cached routes before the data goes
    await ctx.store.truncate_all()
    return await seed(ctx)
