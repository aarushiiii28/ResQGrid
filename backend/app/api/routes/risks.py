from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.api.signing import signed
from app.core.security import Principal, require
from app.schemas.risk import RecoverRequest, Risk
from app.services.context import Ctx

router = APIRouter(prefix="/api/risks", tags=["risks"])
OPS = ("dispatcher", "admin")


@router.post("/evaluate", response_model=list[Risk])
async def evaluate(p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Run the monitor now (deterministic for tests). Returns only newly opened risks."""
    return await ctx.risks.evaluate(actor=p.sub)


@router.get("", response_model=list[Risk])
async def list_risks(status: str | None = None, incident_id: str | None = None,
                     p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    items = await ctx.risks.list(status, incident_id)
    return sorted(items, key=lambda r: (r.status not in ("open", "awaiting"), r.detected_at), reverse=False)[::-1]


@router.get("/{risk_id}", response_model=Risk)
async def get_risk(risk_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    try:
        return await ctx.risks.get(risk_id)
    except KeyError as e:
        raise HTTPException(404, "risk not found") from e


@router.post("/{risk_id}/refresh", response_model=Risk)
async def refresh(risk_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.risks.refresh_options(risk_id)


@router.post("/{risk_id}/recover", response_model=Risk)
async def recover(risk_id: str, body: RecoverRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx),
                  _sig=Depends(signed("risk.recover", "risk.*"))):
    """A human approves one option (with a reason). Plan changes happen only here, never silently."""
    return await ctx.risks.recover(risk_id, body.option_kind, body.reason, p.sub)
