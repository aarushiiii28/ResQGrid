from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx, load_visible_incident
from app.api.signing import signed
from app.core.security import Principal, get_principal, require
from app.schemas.false_alarm import CheckInResponse, CloseRequest, ModelProposal, SignalBatch, SosStart, TagRequest
from app.services.context import Ctx

router = APIRouter(tags=["false-alarm"])
CALLERS = ("caller", "dispatcher", "admin")
OPS = ("dispatcher", "admin")


@router.get("/api/false-alarm/config")
async def fa_config(_: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return ctx.false_alarm.client_config()


@router.post("/api/sos/start", status_code=201)
async def sos_start(body: SosStart, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx),
                    _sig=Depends(signed("sos.start", "incident.pending_created"))):
    """Layer 1: the hold finished and the countdown starts. A PENDING incident is created (not dispatched,
    visible to dispatchers as a muted row). Nothing is ever deleted."""
    inc = await ctx.false_alarm.start_pending(body, p)
    return {"incident_id": inc.id, "status": inc.status, **ctx.false_alarm.client_config(),
            "receipt": await ctx.ledger.receipt(inc.id, "incident.pending_created")}


@router.post("/api/incidents/{incident_id}/sos/confirm")
async def sos_confirm(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx),
                      _sig=Depends(signed("sos.confirm", "incident.went_live"))):
    await load_visible_incident(ctx, incident_id, p)
    inc, call = await ctx.false_alarm.confirm(incident_id, p)
    return {"incident_id": inc.id, "call_id": call.id, "operator": call.transcript[0].text if call.transcript else "",
            "status": inc.status, "mode": "DEMO / SIMULATED", "note": "In-app simulated call. In a real emergency dial 112.",
            "receipt": await ctx.ledger.receipt(inc.id, "incident.went_live")}


@router.post("/api/incidents/{incident_id}/sos/cancel")
async def sos_cancel(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    inc = await ctx.false_alarm.cancel(incident_id, p)
    return {"incident_id": inc.id, "status": inc.status, "note": "Request cancelled. The record is kept. We will check in once."}


@router.post("/api/incidents/{incident_id}/signals")
async def signals(incident_id: str, body: SignalBatch, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    a = await ctx.false_alarm.ingest(incident_id, body)
    return {"incident_id": incident_id, "accepted": len(body.features), "received": "derived numbers only"} | (
        {"lane": a.lane, "probability_genuine": a.probability_genuine} if p.role in OPS else {})


@router.get("/api/incidents/{incident_id}/check-in")
async def check_in_state(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    await ctx.false_alarm.advance(incident_id)  # lazily move the ladder even if the watchdog is slow
    v = await ctx.false_alarm.caller_view(incident_id)
    if v is None:
        raise HTTPException(404, "no check-in for this incident")
    return v


@router.post("/api/incidents/{incident_id}/check-in")
async def check_in_respond(incident_id: str, body: CheckInResponse, p: Principal = Depends(require(*CALLERS)),
                           ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    return await ctx.false_alarm.respond(incident_id, body.response, p=p)


@router.get("/api/false-alarm")
async def fa_list(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.false_alarm.list_views()


@router.get("/api/incidents/{incident_id}/false-alarm")
async def fa_one(incident_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    v = await ctx.false_alarm.view(incident_id)
    if v is None:
        raise HTTPException(404, "no assessment")
    return v


@router.post("/api/incidents/{incident_id}/false-alarm/tag")
async def fa_tag(incident_id: str, body: TagRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    await ctx.false_alarm.tag(incident_id, body.outcome, body.reason, p.sub)
    return await ctx.false_alarm.view(incident_id)


@router.post("/api/incidents/{incident_id}/false-alarm/dispatch")
async def fa_dispatch(incident_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    await ctx.false_alarm.dispatch_now(incident_id, p.sub)
    return await ctx.false_alarm.view(incident_id)


@router.post("/api/incidents/{incident_id}/close")
async def close_incident(incident_id: str, body: CloseRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """A dispatcher closes an incident with a written reason. The record stays; nothing is deleted."""
    await load_visible_incident(ctx, incident_id, p)
    return await ctx.false_alarm.close(incident_id, body.reason, p.sub)


@router.get("/api/false-alarm/model")
async def fa_model(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.false_alarm.model_info()


@router.post("/api/false-alarm/models", status_code=201)
async def fa_propose(body: ModelProposal, p: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    return await ctx.false_alarm.propose_model(body, p.sub)


@router.post("/api/false-alarm/models/{version}/activate")
async def fa_activate(version: str, body: CloseRequest, p: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    """A new model never changes silently: an admin approves it with a reason, and it is audited."""
    return await ctx.false_alarm.activate_model(version, p.sub, body.reason)
