from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.services.context import Ctx

router = APIRouter(prefix="/api", tags=["alerts"])
OPS = ("dispatcher", "admin")


@router.get("/alerts")
async def alerts(p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.alerts.compose()


@router.post("/incidents/{incident_id}/review/resolve")
async def resolve_review(incident_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """A human has checked the extracted/conflicting call details."""
    if await ctx.incidents.get(incident_id) is None:
        raise HTTPException(404, "incident not found")

    def fn(i):
        i.needs_review, i.review_reasons = False, []
        for c in i.conflicts:
            c.resolved = True
        return i

    inc, _ = await ctx.incident_repo.modify(incident_id, fn)
    await ctx.audit.record(entity_type="incident", entity_id=incident_id, event_type="incident.review_resolved",
                           actor=p.sub, correlation_id=incident_id, source="human_confirmed")
    return inc
