from app.services.incident_service import RuleViolation, vis_for


class DriverEventService:
    """Events the driver app reports that are not vehicle statuses."""

    def __init__(self, ctx):
        self.ctx = ctx

    async def arrived(self, vehicle_id: str, actor: str, source: str = "driver") -> dict:
        ctx = self.ctx
        v = await ctx.vehicle_repo.get(vehicle_id)
        if v is None or not v.assigned_incident_id:
            raise RuleViolation("vehicle has no active incident")
        if v.status != "transporting":
            raise RuleViolation(f"vehicle is {v.status}; 'arrived' is reported after transporting starts")
        inc = await ctx.incident_repo.get(v.assigned_incident_id)
        pa = await ctx.cases.for_incident(inc.id)
        if pa is None:
            raise RuleViolation("no pre-alert for this incident")
        await ctx.audit.record(
            entity_type="vehicle", entity_id=vehicle_id, event_type="vehicle.arrived_at_hospital", actor=actor,
            correlation_id=inc.id, source="simulated" if source == "simulated" else "measured",
            payload={"hospital_id": pa.hospital_id}, vis=vis_for(inc) | {"vehicle_id": vehicle_id})
        pa = await ctx.cases.arrival(pa.id, actor, source=source)
        ctx.nav.stop(vehicle_id)
        return {"incident_id": inc.id, "prealert_id": pa.id, "arrived_at": pa.arrived_at}
