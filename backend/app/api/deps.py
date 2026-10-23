from fastapi import HTTPException, Request, status

from app.core.security import Principal
from app.schemas.incident import Incident
from app.services.context import Ctx
from app.services.incident_service import can_view


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


async def load_visible_incident(ctx: Ctx, incident_id: str, p: Principal) -> Incident:
    inc = await ctx.incidents.get(incident_id)
    if inc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    if not can_view(p, inc):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted for this incident")
    return inc
