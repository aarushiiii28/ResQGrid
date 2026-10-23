"""Caller-facing live status: plain text, honest about what is known. Composes assignment, vehicle,
route and hospital state. Locations show their age and source; nothing is invented."""

from app.db.seed import haversine_m
from app.schemas.incident import Incident
from app.services.dispatch_service import category_of

STAGES = [
    ("call_started", "Call started"),
    ("unit_sent", "Ambulance sent"),
    ("acknowledged", "Crew saw the call"),
    ("accepted", "Crew accepted"),
    ("en_route", "On the way"),
    ("on_scene", "At your location"),
    ("transporting", "Taking patient to hospital"),
    ("arrived", "Arrived at hospital"),
]


def fmt_range(lo_s: float, hi_s: float) -> str:
    lo, hi = max(1, round(lo_s / 60)), max(1, round(hi_s / 60))
    return f"{lo} min" if lo == hi else f"{lo}-{hi} min"


class TrackerService:
    def __init__(self, ctx):
        self.ctx = ctx

    async def compose(self, inc: Incident) -> dict:
        ctx = self.ctx
        now = ctx.now()
        pipeline = [e for e in await ctx.audit.list(correlation_id=inc.id, entity_type="pipeline")]
        steps: dict[str, dict] = {}
        for e in pipeline:
            p = e.payload
            steps[p["step"]] = {"step": p["step"], "status": p["status"], "detail": p.get("detail", ""), "at": e.occurred_at}
        asg = [a for a in await ctx.acks.list(inc.id) if a.vehicle_id == inc.assigned_vehicle_id]
        a = asg[-1] if asg else None
        v = await ctx.vehicle_repo.get(inc.assigned_vehicle_id) if inc.assigned_vehicle_id else None
        h = await ctx.hospital_repo.get(inc.destination_id) if inc.destination_id else None
        pa = await ctx.cases.for_incident(inc.id)
        plan = None
        if v and v.status in ("assigned", "accepted", "en_route", "on_scene", "transporting") and h:
            try:
                plan = await ctx.nav.plan(v.id)
            except Exception:
                plan = None
        reached = {"call_started": inc.created_at}
        if a:
            reached["unit_sent"] = a.sent_at
            reached["acknowledged"] = a.acknowledged_at
            reached["accepted"] = a.accepted_at
        hist = {x.detail: x.at for x in (v.history if v else []) if x.kind == "status"}
        for key, label in (("en_route", "-> en_route"), ("on_scene", "-> on_scene"), ("transporting", "-> transporting")):
            for d, at in hist.items():
                if d.endswith(label):
                    reached[key] = at
        if pa and pa.arrived_at:
            reached["arrived"] = pa.arrived_at
        current = None
        stages = []
        for key, label in STAGES:
            at = reached.get(key)
            stages.append({"key": key, "label": label, "state": "done" if at else "pending", "at": at})
        for s in stages:
            if s["state"] == "pending":
                s["state"] = "active"
                current = s["key"]
                break
        # plain-text headline
        eta = fmt_range(plan.remaining_low_s, plan.remaining_high_s) if plan else None
        closed = inc.status == "closed"
        fa = await ctx.false_alarm.view(inc.id)
        stopped = bool(fa and (fa["recalled"] or (fa["checkin"]["state"] == "safe" and not fa["dispatch_started"] and fa["lane"] != "HIGH")))
        if stopped and not closed and not v:
            headline = "You said you are safe, so no ambulance is being sent. A dispatcher will close this request. If you need help, tap I need help or dial 112."
        elif closed:
            headline = "Handover complete. Thank you for staying on the line."
        elif pa and pa.arrived_at:
            headline = f"Arrived at {h.name if h else 'hospital'}."
        elif v and v.status == "transporting":
            headline = f"Heading to {h.name if h else 'hospital'}." + (f" ETA {eta}." if eta else "")
        elif v and v.status == "on_scene":
            headline = "Ambulance is at your location."
        elif v and v.status == "en_route":
            headline = f"Ambulance on the way. ETA {eta}." if eta else "Ambulance on the way."
        elif a and a.state == "accepted":
            headline = f"Ambulance accepted. ETA {eta}." if eta else "Ambulance accepted."
        elif a and a.state == "timed_out":
            headline = "No reply from the crew yet. A dispatcher has been alerted."
        elif a and a.state == "rejected":
            headline = "Crew could not take the call. Finding another ambulance."
        elif a and a.state == "acknowledged":
            headline = "Crew has seen the call. Waiting for them to accept."
        elif a:
            headline = "Ambulance sent. Not yet confirmed by the crew."
        elif steps.get("approval", {}).get("status") == "waiting":
            headline = "Waiting for a dispatcher to approve the ambulance."
        elif steps.get("dispatch_recommendation", {}).get("status") == "failed":
            headline = "No ambulance available right now. A dispatcher has been alerted."
        elif steps.get("intake", {}).get("status") == "waiting":
            headline = "Tell us what happened. We choose the right ambulance as soon as you answer."
        else:
            headline = "Finding the nearest ambulance."
        loc = inc.location_candidates[0] if inc.location_candidates else None
        vehicle = None
        if v and v.location:
            age = (now - v.location_at).total_seconds() if v.location_at else None
            vehicle = {"id": v.id, "lat": v.location.lat, "lon": v.location.lon, "location_age_s": age,
                       "source": v.location.source.value, "status": v.status,
                       "state": "stale" if (age is None or age > ctx.settings.vehicle_stale_s) else "live",
                       "simulated": v.location.source.value == "simulated"}
        nearby = []
        if loc:
            idle = [x for x in await ctx.vehicle_repo.list() if x.status == "available" and x.location]
            ranked = sorted(((haversine_m((loc.lat, loc.lon), (x.location.lat, x.location.lon)), x) for x in idle),
                            key=lambda t: t[0])
            nearby = [{"id": x.id, "lat": x.location.lat, "lon": x.location.lon, "distance_m": round(d),
                       "tags": x.capability_tags, "simulated": x.location.source.value == "simulated"}
                      for d, x in ranked[:4] if d <= 15_000]
        return {
            "nearby": nearby,
            "incident_id": inc.id, "status": inc.status, "headline": headline, "stages": stages, "current_stage": current,
            "category": category_of(inc), "needs_review": inc.needs_review,
            "location": None if not loc else {"lat": loc.lat, "lon": loc.lon, "accuracy_m": loc.accuracy_m,
                                              "approximate": loc.approximate, "source": inc.location_source},
            "vehicle": vehicle,
            "route": None if not plan else {"polyline": plan.route.polyline, "phase": plan.phase, "degraded": plan.route.degraded,
                                            "source": plan.route.source},
            "eta": None if not plan else {"low_s": plan.remaining_low_s, "high_s": plan.remaining_high_s, "text": eta,
                                          "phase": plan.phase},
            "hospital": None if not h else {"id": h.id, "name": h.name, "lat": h.location.lat, "lon": h.location.lon},
            "assignment": None if not a else {"id": a.id, "state": a.state, "late": a.late},
            "dispatch_stopped": stopped and not v,
            "pipeline": [steps[k] for k in ("incident_update", "intake", "extraction", "dispatch_recommendation", "approval",
                                            "hospital_match", "pre_alert") if k in steps],
            "autopilot": ctx.autopilot.status(),
        }
