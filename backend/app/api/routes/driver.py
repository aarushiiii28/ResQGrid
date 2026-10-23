from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_ctx
from app.api.signing import signed
from app.core.security import Principal, require
from app.services.context import Ctx
from app.services.vehicle_service import make_view

router = APIRouter(prefix="/api/driver", tags=["driver"])
OPS = ("dispatcher", "admin")


def _own(p: Principal, vid: str) -> None:
    if p.role == "driver" and p.vehicle_id != vid:
        raise HTTPException(403, "drivers may only use their own vehicle")


@router.get("/me")
async def me(p: Principal = Depends(require("driver")), ctx: Ctx = Depends(get_ctx)):
    """Everything the driver app needs in one call (assignment, brief incident summary, nav plan)."""
    v = await ctx.vehicle_repo.get(p.vehicle_id)
    view = make_view(v, ctx.now(), ctx.settings.vehicle_stale_s)
    inc = await ctx.incident_repo.get(v.assigned_incident_id) if v.assigned_incident_id else None
    asg = None
    if inc:
        mine = [a for a in await ctx.acks.list(inc.id) if a.vehicle_id == v.id]
        asg = mine[-1] if mine else None
    plan = None
    if inc and v.location:
        try:
            plan = await ctx.nav.plan(v.id)
        except Exception:  # noqa: BLE001  (e.g. no destination yet)
            plan = None
    h = await ctx.hospital_repo.get(inc.destination_id) if inc and inc.destination_id else None
    pa = await ctx.cases.for_incident(inc.id) if inc else None
    summary = None
    if inc:
        loc = inc.location_candidates[0] if inc.location_candidates else None
        summary = {
            "id": inc.id, "status": inc.status,
            "category": inc.reported_category.value if inc.reported_category else None,
            "category_source": inc.reported_category.source.value if inc.reported_category else None,
            "priority": inc.operator_priority.value if inc.operator_priority else None,
            "patient_count": inc.patient_count.value if inc.patient_count else None,
            "unknowns": inc.unknowns,
            "arrived": bool(pa and pa.arrived_at),
            "handover_status": pa.status if pa else None,
            "location": None if not loc else {"lat": loc.lat, "lon": loc.lon, "accuracy_m": loc.accuracy_m, "approximate": loc.approximate},
            "hospital": None if not h else {"id": h.id, "name": h.name, "lat": h.location.lat, "lon": h.location.lon},
        }
    recalled = None
    if inc is None:  # tell a crew that was stood down why, instead of silently showing "standing by"
        now = ctx.now()
        gone = [a for a in await ctx.acks.list() if a.vehicle_id == v.id and a.state == "cancelled" and a.cancelled_at
                and (now - a.cancelled_at).total_seconds() < 600]
        if gone:
            a = max(gone, key=lambda x: x.cancelled_at)
            recalled = {"incident_id": a.incident_id, "reason": a.history[-1].note if a.history else None, "at": a.cancelled_at}
    return {"vehicle": view, "assignment": asg, "incident": summary, "plan": plan, "recalled": recalled}


@router.get("/{vehicle_id}/plan")
async def plan(vehicle_id: str, p: Principal = Depends(require("driver", *OPS)), ctx: Ctx = Depends(get_ctx)):
    _own(p, vehicle_id)
    pl = await ctx.nav.plan(vehicle_id)
    if pl is None:
        raise HTTPException(404, "no active assignment with a known position")
    return pl


@router.post("/{vehicle_id}/simulate-movement")
async def simulate_movement(vehicle_id: str, p: Principal = Depends(require("driver", *OPS)), ctx: Ctx = Depends(get_ctx)):
    """Demo: move this vehicle along its active route with simulated GPS (labelled simulated)."""
    _own(p, vehicle_id)
    if await ctx.nav.plan(vehicle_id) is None:
        raise HTTPException(409, "no active assignment")
    ctx.nav.start_drive(vehicle_id)
    await ctx.audit.record(entity_type="vehicle", entity_id=vehicle_id, event_type="vehicle.simulated_movement_started",
                           actor=p.sub, source="simulated")
    return {"started": True, "label": "Simulated GPS movement"}


@router.post("/{vehicle_id}/stop-movement")
async def stop_movement(vehicle_id: str, p: Principal = Depends(require("driver", *OPS)), ctx: Ctx = Depends(get_ctx)):
    _own(p, vehicle_id)
    ctx.nav.stop(vehicle_id)
    return {"stopped": True}


@router.post("/{vehicle_id}/arrived")
async def arrived(vehicle_id: str, p: Principal = Depends(require("driver", *OPS)), ctx: Ctx = Depends(get_ctx),
                  _sig=Depends(signed("driver.arrived", "vehicle.*"))):
    _own(p, vehicle_id)
    return await ctx.driver_events.arrived(vehicle_id, p.sub, source="driver")


@router.post("/{vehicle_id}/route-notice/{notice_id}/ack")
async def ack_route_notice(vehicle_id: str, notice_id: str, p: Principal = Depends(require("driver")), ctx: Ctx = Depends(get_ctx),
                           _sig=Depends(signed("reroute.ack", "nav.*"))):
    """The driver saw an automatic same-destination route change. This is an acknowledgment, not an approval."""
    _own(p, vehicle_id)
    return await ctx.stability.acknowledge(vehicle_id, notice_id, p.sub)


@router.post("/{vehicle_id}/simulate-wrong-turn")
async def simulate_wrong_turn(vehicle_id: str, offset_m: float = 220.0, p: Principal = Depends(require("driver", *OPS)),
                              ctx: Ctx = Depends(get_ctx)):
    """Demo only (labelled simulated): push the simulated GPS sideways so the stable-route policy can show a rejoin."""
    _own(p, vehicle_id)
    if not 60 <= offset_m <= 1000:
        raise HTTPException(422, "offset_m must be between 60 and 1000")
    return await ctx.stability.simulate_wrong_turn(vehicle_id, offset_m)


@router.post("/{vehicle_id}/reroute/{proposal_id}/{action}")
async def reroute(vehicle_id: str, proposal_id: str, action: str, p: Principal = Depends(require("driver")),
                  ctx: Ctx = Depends(get_ctx), _sig=Depends(signed("reroute.{action}", "nav.*"))):
    _own(p, vehicle_id)
    if action not in ("accept", "decline"):
        raise HTTPException(404, "unknown action")
    return await ctx.nav.respond_reroute(vehicle_id, proposal_id, action == "accept", p.sub)
