from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_ctx, load_visible_incident
from app.core.security import Principal, get_principal, require
from app.schemas.common import Source
from app.schemas.incident import (
    FactSet,
    IncidentCreate,
    MergeRequest,
    ReportCreate,
    SplitRequest,
)
from app.services.audit_service import AuditService
from app.services.context import Ctx

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

OPERATOR = ("dispatcher", "admin")


@router.post("", status_code=201)
async def create_incident(body: IncidentCreate, p: Principal = Depends(require("caller", *OPERATOR)),
                          ctx: Ctx = Depends(get_ctx)):
    return await ctx.incidents.create(body, p)


@router.get("")
async def list_incidents(p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    items = await ctx.incidents.list_for(p)
    return sorted(items, key=lambda i: i.created_at, reverse=True)


@router.get("/{incident_id}")
async def incident_detail(incident_id: str, p: Principal = Depends(get_principal),
                          ctx: Ctx = Depends(get_ctx)):
    inc = await load_visible_incident(ctx, incident_id, p)
    out = {"incident": inc, "reports": await ctx.incidents.reports_for(incident_id)}
    if p.role in OPERATOR:
        out["timeline"] = await ctx.audit.list(correlation_id=incident_id)
    return out


@router.post("/{incident_id}/reports", status_code=201)
async def attach_report(incident_id: str, body: ReportCreate,
                        p: Principal = Depends(require("caller", *OPERATOR)),
                        ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    report, suggestions = await ctx.incidents.add_report(incident_id, body, p)
    return {"report": report, "duplicate_suggestions": suggestions, "auto_merged": False}


@router.post("/{incident_id}/facts")
async def set_fact(incident_id: str, body: FactSet, p: Principal = Depends(require(*OPERATOR)),
                   ctx: Ctx = Depends(get_ctx)):
    if body.source in (Source.ai_extracted, Source.simulated):
        raise HTTPException(422, "operators set reported, measured or human_confirmed facts")
    await load_visible_incident(ctx, incident_id, p)
    return await ctx.incidents.set_fact(
        incident_id, body.key, body.value, source=body.source, confidence=body.confidence,
        actor=p.sub, evidence=body.evidence, reason=body.reason,
    )


@router.post("/{incident_id}/merge")
async def merge(incident_id: str, body: MergeRequest, p: Principal = Depends(require(*OPERATOR)),
                ctx: Ctx = Depends(get_ctx)):
    """Human-confirmed merge of `incident_id` into `target_id`. Reversible via /unmerge."""
    return await ctx.incidents.merge(incident_id, body.target_id, p.sub, body.reason)


@router.post("/{incident_id}/unmerge")
async def unmerge(incident_id: str, reason: str = Query(min_length=3),
                  p: Principal = Depends(require(*OPERATOR)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.incidents.unmerge(incident_id, p.sub, reason)


@router.post("/{incident_id}/split", status_code=201)
async def split(incident_id: str, body: SplitRequest, p: Principal = Depends(require(*OPERATOR)),
                ctx: Ctx = Depends(get_ctx)):
    return await ctx.incidents.split(incident_id, body.report_ids, p.sub, body.reason)


@router.get("/{incident_id}/audit")
async def export_audit(incident_id: str, format: str = Query("json", pattern="^(json|csv)$"),
                       p: Principal = Depends(require(*OPERATOR)), ctx: Ctx = Depends(get_ctx)):
    if await ctx.incidents.get(incident_id) is None:
        raise HTTPException(404, "incident not found")
    events = await ctx.audit.list(correlation_id=incident_id)
    if format == "csv":
        return Response(AuditService.to_csv(events), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{incident_id}-audit.csv"'})
    return Response(AuditService.to_json(events), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{incident_id}-audit.json"'})
