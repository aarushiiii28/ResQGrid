"""Operational alerts strip for the command center. Derived from live state, never invented."""

from app.services.vehicle_service import make_view as vehicle_view


class AlertsService:
    def __init__(self, ctx):
        self.ctx = ctx

    async def compose(self) -> list[dict]:
        ctx, s = self.ctx, self.ctx.settings
        now = ctx.now()
        out: list[dict] = []

        def add(kind: str, sev: str, text: str, entity: str, incident_id: str | None = None, pulse: bool = False):
            out.append({"id": f"{kind}:{entity}", "kind": kind, "severity": sev, "text": text, "entity": entity,
                        "incident_id": incident_id, "pulse": pulse})

        for a in await ctx.acks.list():
            if a.state == "timed_out":
                add("no_acknowledgment", "red", f"No acceptance from {a.vehicle_id} for {a.incident_id}. {a.fallback['message'] if a.fallback else ''}".strip(),
                    a.id, a.incident_id, True)
            elif a.state == "sent" and a.sent_at and (now - a.sent_at).total_seconds() > a.timeout_s / 2:
                add("unacknowledged", "amber", f"{a.vehicle_id} has not acknowledged {a.incident_id} yet (sent, not received).",
                    a.id, a.incident_id, True)
        for d in await ctx.dispatch.repo.list():
            if d.status == "pending":
                add("awaiting_approval", "amber", f"Recommendation for {d.incident_id} awaits approval.", d.id, d.incident_id)
        busy = {"assigned", "accepted", "en_route", "on_scene", "transporting"}
        for v in await ctx.vehicle_repo.list():
            view = vehicle_view(v, now, s.vehicle_stale_s)
            if view.location_state == "stale" and v.status in busy:
                add("stale_gps", "red", f"{v.id} position is {int(view.location_age_s or 0)}s old. Shown as last-known, not live.",
                    v.id, v.assigned_incident_id)
            elif view.offline:
                add("offline", "amber", f"{v.id} is offline.", v.id, v.assigned_incident_id)
        for vid, st in ctx.nav.state.items():
            p = st.get("proposal")
            if p and p.status == "pending":
                add("reroute_pending", "amber", f"{vid}: reroute proposed ({p.reason}). Waiting for the driver.", p.id)
            n = (st.get("stab") or {}).get("notice")
            if n is not None and (not n.acknowledged) and (now - n.at).total_seconds() < 600:
                saved = f"{abs(n.saved_s) / 60:.1f} min {'saved' if n.saved_s > 0 else 'longer, but necessary'}"
                add("route_changed", "amber", f"{vid}: route changed automatically ({n.reason.replace('_', ' ')}, {n.kind}), {saved}. Same destination. Driver has not acknowledged yet.", n.id)
        used = {i.destination_id for i in await ctx.incident_repo.list() if i.destination_id and i.status != "closed"}
        for h in await ctx.hospitals.list_views():
            if h.id in used and any(r.stale and r.reported_status == "available" for r in h.resources.values()):
                add("stale_hospital", "amber", f"{h.name}: capacity data is stale. Treated as unknown.", h.id)
        for i in await ctx.incident_repo.list():
            if i.needs_review and i.status not in ("closed", "merged", "cancelled_by_caller", "pending"):
                add("needs_review", "amber", f"{i.id}: call details need human review ({', '.join(i.review_reasons)}).", i.id, i.id)
        for fa in await ctx.false_alarm.list_views():
            if fa["close_reason"] or fa["tag"]:
                continue
            if fa["checkin"]["state"] == "unanswered":
                add("checkin_unanswered", "red", f"{fa['incident_id']}: no answer to the check-in"
                    + (" after the caller cancelled the SOS. Check on them." if fa["status"] == "cancelled_by_caller" else ". Escalated, not dismissed."),
                    fa["incident_id"], fa["incident_id"], True)
            elif fa["hold_remaining_s"] is not None:
                add("low_lane_verifying", "amber", f"{fa['incident_id']}: low-probability SOS, verifying ({int(fa['hold_remaining_s'])} s left). Dispatch now if in doubt.",
                    fa["incident_id"], fa["incident_id"])
            elif fa["awaiting_review"]:
                add("caller_says_safe", "amber", f"{fa['incident_id']}: caller says safe. Close with a reason, or dispatch.",
                    fa["incident_id"], fa["incident_id"])
        for pa in await ctx.cases.repo.list():
            if pa.status == "incoming" and (now - pa.created_at).total_seconds() > 60:
                add("prealert_unacked", "amber", f"{pa.hospital_id} has not acknowledged pre-alert {pa.id}.", pa.id, pa.incident_id)
        for r in await ctx.risks.open_risks():
            add("risk", "red" if r.severity in ("critical", "high") else "amber",
                f"{r.type.replace('_', ' ')} on {r.incident_id}: {r.affected_assumption}. Recovery options need approval ({r.id}).",
                r.id, r.incident_id, r.status == "open" and r.severity == "critical")
        rank = {"red": 0, "amber": 1}
        return sorted(out, key=lambda a: (rank.get(a["severity"], 2), a["id"]))
