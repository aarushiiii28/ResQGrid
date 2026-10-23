from fastapi import APIRouter, Depends

from app.api.deps import get_ctx
from app.services.context import Ctx

router = APIRouter()


@router.get("/health")
async def health(ctx: Ctx = Depends(get_ctx)) -> dict:
    db_ok = await ctx.store.ping()
    s = ctx.settings
    return {
        "status": "ok" if db_ok else "degraded",
        "mode": "DEMO / SIMULATED",
        "database": "ok" if db_ok else "unreachable",
        "routing_provider": s.routing_provider,
        "demo_autopilot": ctx.autopilot.enabled,
        "event_seq": ctx.hub.seq,
    }


@router.get("/api/public/summary")
async def summary(ctx: Ctx = Depends(get_ctx)) -> dict:
    """Counts only, for the landing page. Synthetic data; no identities."""
    vehicles = await ctx.vehicles.list_views()
    hospitals = await ctx.hospitals.list_views()
    incidents = await ctx.incident_repo.list()
    return {
        "units_total": len(vehicles),
        "units_available": sum(v.availability == "available" for v in vehicles),
        "units_unknown": sum(v.availability == "unknown" for v in vehicles),
        "hospitals_total": len(hospitals),
        "hospitals_fresh": sum(not any(r.stale and r.reported_status == "available" for r in h.resources.values()) for h in hospitals),
        "open_incidents": sum(i.status not in ("closed", "merged", "cancelled_by_caller", "pending") for i in incidents),
        "mode": "DEMO / SIMULATED",
    }
