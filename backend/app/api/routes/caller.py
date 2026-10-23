import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx, load_visible_incident
from app.api.signing import signed
from app.core.security import Principal, get_principal, require
from app.schemas.call import LocationUpdate, SosRequest, Utterance
from app.services.context import Ctx
from app.services.incident_service import can_view

router = APIRouter(tags=["caller"])
CALLERS = ("caller", "dispatcher", "admin")
OPS = ("dispatcher", "admin")
FIRST_AID = json.loads((Path(__file__).resolve().parents[2] / "content" / "first_aid.json").read_text(encoding="utf-8"))


@router.post("/api/sos", status_code=201)
async def sos(body: SosRequest, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx),
              _sig=Depends(signed("sos.start", "incident.created"))):
    """One press, nothing else: creates the incident from the caller's location and opens the
    simulated call. Dispatch, approval, hospital match and pre-alert run automatically."""
    inc, call = await ctx.calls.sos(body, p, device_id=body.device_id)
    return {"incident_id": inc.id, "call_id": call.id, "operator": call.transcript[0].text,
            "mode": "DEMO / SIMULATED", "note": "In-app simulated call. In a real emergency dial 112.",
            "receipt": await ctx.ledger.receipt(inc.id, "incident.created")}


@router.post("/api/incidents/{incident_id}/location")
async def update_location(incident_id: str, body: LocationUpdate, p: Principal = Depends(require(*CALLERS)),
                          ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    inc = await ctx.calls.update_location(incident_id, body, p)
    return {"incident_id": inc.id, "unknowns": inc.unknowns, "location": inc.location_candidates[0]}


async def _own_call(ctx: Ctx, call_id: str, p: Principal):
    try:
        call = await ctx.calls.get(call_id)
    except KeyError as e:
        raise HTTPException(404, "call not found") from e
    inc = await ctx.incidents.get(call.incident_id)
    if inc is None or not can_view(p, inc):
        raise HTTPException(403, "not permitted for this call")
    return call


@router.get("/api/calls/{call_id}")
async def get_call(call_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    return await _own_call(ctx, call_id, p)


@router.post("/api/calls/{call_id}/utterances")
async def utterance(call_id: str, body: Utterance, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await _own_call(ctx, call_id, p)
    return await ctx.calls.utterance(call_id, body, p)


@router.post("/api/calls/{call_id}/end")
async def end_call(call_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await _own_call(ctx, call_id, p)
    return await ctx.calls.end(call_id, p)


@router.get("/api/tracker/{incident_id}")
async def tracker(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    inc = await load_visible_incident(ctx, incident_id, p)
    return await ctx.tracker.compose(inc)


@router.get("/api/incidents/{incident_id}/aed")
async def aed(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    inc = await load_visible_incident(ctx, incident_id, p)
    return await ctx.volunteers.nearest_aeds(inc)


@router.get("/api/incidents/{incident_id}/volunteer-alerts")
async def volunteer_alerts(incident_id: str, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    await load_visible_incident(ctx, incident_id, p)
    return await ctx.volunteers.list_for(incident_id)


@router.post("/api/incidents/{incident_id}/volunteer-alerts", status_code=201)
async def trigger_volunteer_alerts(incident_id: str, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    inc = await load_visible_incident(ctx, incident_id, p)
    return await ctx.volunteers.alert(inc, p.sub)


@router.post("/api/volunteer-alerts/{alert_id}/respond")
async def respond_volunteer(alert_id: str, accept: bool, p: Principal = Depends(require(*CALLERS)), ctx: Ctx = Depends(get_ctx)):
    """Simulates the synthetic volunteer's reply (accept or decline). No real person is contacted."""
    a = await ctx.volunteers.repo.get(alert_id)
    if a is None:
        raise HTTPException(404, "alert not found")
    await load_visible_incident(ctx, a.incident_id, p)
    return await ctx.volunteers.respond(alert_id, accept, f"simulated-volunteer(via {p.sub})")


@router.get("/api/first-aid")
async def first_aid(_: Principal = Depends(get_principal)):
    return FIRST_AID
