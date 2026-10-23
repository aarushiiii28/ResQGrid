from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.schemas.mci import ApproveBody, Mci, MciCreate, SuggestBody, TriageSet
from app.services.context import Ctx

router = APIRouter(tags=["mass-casualty and demand"])
OPS = ("dispatcher", "admin")


@router.post("/api/mci", response_model=Mci, status_code=201)
async def create_mci(body: MciCreate, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.mci.create(body.incident_id, body.patients, p.sub)


@router.get("/api/mci", response_model=list[Mci])
async def list_mci(incident_id: str | None = None, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return [await ctx.mci.live(m) for m in await ctx.mci.list(incident_id)]


@router.get("/api/mci/{mci_id}", response_model=Mci)
async def get_mci(mci_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    try:
        return await ctx.mci.live(await ctx.mci.get(mci_id))
    except KeyError as e:
        raise HTTPException(404, "mass-casualty record not found") from e


@router.patch("/api/mci/{mci_id}/patients/{pid}", response_model=Mci)
async def set_triage(mci_id: str, pid: str, body: TriageSet, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """A human sets or confirms the triage colour. Suggestions never count until this is called."""
    return await ctx.mci.set_triage(mci_id, pid, body.triage, p.sub, body.note)


@router.post("/api/mci/{mci_id}/suggest", response_model=Mci)
async def suggest(mci_id: str, body: SuggestBody, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.mci.suggest(mci_id, body.text, p.sub)


@router.post("/api/mci/{mci_id}/plan", response_model=Mci)
async def plan(mci_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Split patients across hospitals and assign ambulances. Does not commit anything."""
    return await ctx.mci.plan(mci_id, p.sub)


@router.post("/api/mci/{mci_id}/approve", response_model=Mci)
async def approve(mci_id: str, body: ApproveBody, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.mci.approve(mci_id, body.reason, p.sub)


@router.get("/api/demand/forecast")
async def forecast(bucket: int | None = Query(None, ge=0, le=5), daytype: str | None = Query(None, pattern="^(auto|weekday|weekend)$"),
                   standby: int | None = Query(None, ge=1, le=12), p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Demand forecast, heat points and suggested standby points. Prototype approximation."""
    return await ctx.demand.forecast(bucket, daytype, standby)
