"""M15 mass-casualty mode. Humans set (or confirm) triage colours; the planner splits patients across
hospitals by capability and remaining capacity (OR-Tools), then assigns several ambulances (OR-Tools,
compared with greedy). Nothing is dispatched until a human approves the plan. Colours are routing
labels entered by people; this is not clinical triage software."""

import re
from datetime import datetime

from app.db.repos import Repo
from app.optimization.mci_solver import HospitalCap, greedy_split, solve_split
from app.optimization.ortools_solver import greedy, ortools_assign
from app.schemas.mci import HospitalLoad, Load, Mci, McPlan, Patient
from app.schemas.vehicle import VehicleStateUpdate
from app.services.incident_service import RuleViolation, vis_for

RULES = [
    "Red and unconfirmed patients need a trauma-capable hospital with an available trauma team and an ICU bed (configuration, not clinical advice).",
    "Amber and green patients need an emergency department with an available ER bay.",
    "Black is never transported by the planner; it is recorded and counted.",
    "Capacity comes from fresh, available resource counts only. Stale or unknown resources count as zero.",
    "An ambulance carries one red patient, or up to two amber or green patients.",
]
PER_LOAD = {"red": 1, "unassigned": 1, "amber": 2, "green": 2}
PLAN_TRIAGE = ("red", "amber", "green", "unassigned")


class MciService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[Mci] = Repo(ctx.store, "mci", Mci)

    async def get(self, mci_id: str) -> Mci:
        m = await self.repo.get(mci_id)
        if m is None:
            raise KeyError(mci_id)
        return m

    async def create(self, incident_id: str, n: int, actor: str) -> Mci:
        ctx = self.ctx
        inc = await ctx.incident_repo.get(incident_id)
        if inc is None:
            raise KeyError(incident_id)
        if any(m.incident_id == incident_id for m in await self.repo.list()):
            raise RuleViolation("this incident already has a mass-casualty record")
        k = await ctx.store.next_seq("mci_seq")
        m = Mci(id=f"MCI-{k:03d}", incident_id=incident_id, created_at=ctx.now(), created_by=actor,
                patients=[Patient(id=f"P-{i + 1:02d}") for i in range(n)])
        await self.repo.put(m)
        await ctx.incidents.set_fact(incident_id, "patient_count", n, source=__import__("app.schemas.common", fromlist=["Source"]).Source.human_confirmed,
                                     confidence=1.0, actor=actor, reason="mass-casualty record opened")
        await ctx.audit.record(entity_type="mci", entity_id=m.id, event_type="mci.created", actor=actor, correlation_id=incident_id,
                               source="human_confirmed", new={"patients": n}, vis=vis_for(inc))
        return m

    async def list(self, incident_id: str | None = None) -> list[Mci]:
        return [m for m in await self.repo.list() if incident_id is None or m.incident_id == incident_id]

    async def set_triage(self, mci_id: str, pid: str, triage: str, actor: str, note: str | None = None) -> Mci:
        m = await self.get(mci_id)
        if m.status == "approved":
            raise RuleViolation("the plan is already approved; changes need a new plan")
        old: dict = {}

        def fn(x: Mci) -> Mci:
            p = next((q for q in x.patients if q.id == pid), None)
            if p is None:
                raise RuleViolation(f"unknown patient {pid}")
            old.update(triage=p.triage, confirmed=p.confirmed)
            p.triage, p.confirmed, p.set_by, p.note = triage, True, actor, note  # type: ignore[assignment]
            if p.suggested == triage:
                p.suggested = None
            x.plan, x.status = None, "triage"  # any change invalidates the plan
            return x

        m, _ = await self.repo.modify(mci_id, fn)
        await self.ctx.audit.record(entity_type="mci", entity_id=mci_id, event_type="mci.triage_set", actor=actor,
                                    correlation_id=m.incident_id, source="human_confirmed", old=old,
                                    new={"triage": triage, "confirmed": True}, payload={"patient": pid, "note": note})
        return m

    async def suggest(self, mci_id: str, text: str, actor: str) -> Mci:
        """Extract colour counts from words as SUGGESTIONS. They count for nothing until a human confirms."""
        counts = {c: 0 for c in ("red", "amber", "green", "black")}
        evidence: dict[str, str] = {}
        for n, colour in re.findall(r"\b(\d{1,3})\s*(red|amber|yellow|green|black)\b", text.lower()):
            c = "amber" if colour == "yellow" else colour
            counts[c] += int(n)
            evidence[c] = f"{n} {colour}"

        def fn(x: Mci) -> Mci:
            free = [p for p in x.patients if not p.confirmed]
            i = 0
            for colour, k in counts.items():
                for _ in range(k):
                    if i < len(free):
                        free[i].suggested, free[i].suggested_evidence = colour, evidence[colour]  # type: ignore[assignment]
                        i += 1
            return x

        m, _ = await self.repo.modify(mci_id, fn)
        await self.ctx.audit.record(entity_type="mci", entity_id=mci_id, event_type="mci.triage_suggested", actor=actor,
                                    correlation_id=m.incident_id, source="ai_extracted", payload={"counts": counts, "text": text[:200]})
        return m

    # ---- plan ---------------------------------------------------------------------------------------------
    async def plan(self, mci_id: str, actor: str) -> Mci:
        ctx = self.ctx
        m = await self.get(mci_id)
        inc = await ctx.incident_repo.get(m.incident_id)
        disr = await ctx.routing.active_disruptions()
        scene, _, _ = await ctx.dispatch.scene_point(inc, disr)
        transport = [(p.id, p.triage if p.confirmed else "unassigned") for p in m.patients if not (p.confirmed and p.triage == "black")]
        unconfirmed = [p.id for p in m.patients if not p.confirmed]
        warnings: list[str] = []
        caps: list[HospitalCap] = []
        meta: dict[str, dict] = {}
        for h in await ctx.hospital_repo.list():
            v = ctx.hospitals.view(h)
            er, icu, tt = v.resources.get("er_bay"), v.resources.get("icu_bed"), v.resources.get("trauma_team")
            er_n = (er.count if er.count is not None else 1) if er and er.status == "available" else 0
            icu_n = (icu.count if icu.count is not None else 1) if icu and icu.status == "available" else 0
            red_ok = "trauma" in h.capabilities and tt is not None and tt.status == "available"
            unverified = [n for n, r in v.resources.items() if n in ("er_bay", "icu_bed", "trauma_team") and r.status == "unknown"]
            r = await ctx.routing.route(scene, (h.location.lat, h.location.lon), disr)
            accepts = {"amber", "green"} | ({"red", "unassigned"} if red_ok else set())
            caps.append(HospitalCap(h.id, er_n, min(icu_n, er_n) if red_ok else 0, r.duration_s / 60, accepts))
            meta[h.id] = {"name": h.name, "unverified": unverified, "eta_s": r.duration_s, "caps": h.capabilities}
        opt = solve_split(transport, caps)
        base = greedy_split(transport, caps)
        if opt.overflow:
            warnings.append(f"{len(opt.overflow)} patient(s) exceed the capacity of every eligible hospital: request mutual aid or divert (simulated escalation).")
        if unconfirmed:
            warnings.append(f"{len(unconfirmed)} patient(s) have no confirmed triage colour and are planned as red until a human confirms.")
        # loads: reds one by one, ambers and greens two per ambulance
        loads: list[Load] = []
        by_h: dict[str, list[tuple[str, str]]] = {}
        for pid, tri in transport:
            if pid in opt.assignment:
                by_h.setdefault(opt.assignment[pid], []).append((pid, tri))
        n = 0
        for hid in sorted(by_h):
            group = sorted(by_h[hid], key=lambda x: ({"red": 0, "unassigned": 0, "amber": 1, "green": 2}[x[1]], x[0]))
            singles = [g for g in group if g[1] in ("red", "unassigned")]
            rest = [g for g in group if g[1] not in ("red", "unassigned")]
            chunks = [[g] for g in singles] + [rest[i:i + 2] for i in range(0, len(rest), 2)]
            for ch in chunks:
                n += 1
                mix: dict[str, int] = {}
                for _, t in ch:
                    mix[t] = mix.get(t, 0) + 1
                needs = ["BLS"] if all(t == "green" for _, t in ch) else ["ALS"]
                loads.append(Load(id=f"LOAD-{n}", patient_ids=[p for p, _ in ch], triage_mix=mix, hospital_id=hid,
                                  vehicle_needs=needs, eta_hospital_s=meta[hid]["eta_s"]))
        cost: dict[tuple[str, str], float] = {}
        scene_eta: dict[tuple[str, str], float] = {}
        for ld in loads:
            for v, ok, _ in await ctx.vehicles.eligible(set(ld.vehicle_needs)):
                if not ok or v.location is None:
                    continue
                r = await ctx.routing.route((v.location.lat, v.location.lon), scene, disr)
                cost[(ld.id, v.id)] = round(r.duration_s / 60 + 0.3 * ld.eta_hospital_s / 60, 3)
                scene_eta[(ld.id, v.id)] = r.duration_s
        order = [ld.id for ld in sorted(loads, key=lambda x: (0 if "red" in x.triage_mix or "unassigned" in x.triage_mix else 1, x.id))]
        # keep the cheapest few vehicles per load so the model stays small
        keep: dict[tuple[str, str], float] = {}
        for ld in loads:
            best = sorted((c, vid) for (lid, vid), c in cost.items() if lid == ld.id)[:8]
            for c, v in best:
                keep[(ld.id, v)] = c
        g_sol, o_sol = greedy(order, keep), ortools_assign(order, keep)
        for ld in loads:
            v = o_sol.assignment.get(ld.id)
            if v:
                ld.vehicle_id, ld.eta_scene_s = v, scene_eta[(ld.id, v)]
            else:
                warnings.append(f"No free unit for {ld.id} ({', '.join(f'{k} {c}' for k, c in ld.triage_mix.items())}): request mutual aid (simulated).")
        hl: list[HospitalLoad] = []
        for c in caps:
            planned_ids = [p for p, h in opt.assignment.items() if h == c.id]
            reds = sum(1 for p, t in transport if p in planned_ids and t in ("red", "unassigned"))
            note = None
            if meta[c.id]["unverified"]:
                note = "capacity data unverified: counted as zero until the hospital confirms"
            hl.append(HospitalLoad(hospital_id=c.id, name=meta[c.id]["name"], capacity=c.er, red_capacity=c.icu, planned=len(planned_ids),
                                   planned_red=reds, fraction=round(len(planned_ids) / max(c.er, 1), 3) if c.er else 0.0,
                                   unverified_resources=meta[c.id]["unverified"], note=note))
        hl.sort(key=lambda h: (-h.planned, h.hospital_id))
        plan = McPlan(
            created_at=ctx.now(), patient_hospital=opt.assignment, overflow=opt.overflow, loads=loads, hospital_loads=hl,
            unconfirmed=unconfirmed, warnings=warnings, rules=RULES,
            split={"ortools": {"cost": opt.total_cost, "assigned": len(opt.assignment), "overflow": len(opt.overflow), "status": opt.status},
                   "greedy": {"cost": base.total_cost, "assigned": len(base.assignment), "overflow": len(base.overflow)}},
            assignment={"ortools": {"cost": o_sol.total_cost, "assigned": len(o_sol.assignment), "unassigned": o_sol.unassigned, "status": o_sol.status},
                        "greedy": {"cost": g_sol.total_cost, "assigned": len(g_sol.assignment), "unassigned": g_sol.unassigned}})

        def fn(x: Mci) -> Mci:
            x.plan, x.status = plan, "planned"
            return x

        m, _ = await self.repo.modify(mci_id, fn)
        await ctx.audit.record(entity_type="mci", entity_id=mci_id, event_type="mci.planned", actor=actor, correlation_id=m.incident_id,
                               source="simulated", payload={"loads": len(loads), "overflow": len(opt.overflow),
                                                            "split_cost": opt.total_cost, "greedy_split_cost": base.total_cost,
                                                            "assignment_cost": o_sol.total_cost, "greedy_assignment_cost": g_sol.total_cost})
        return m

    async def live(self, m: Mci) -> Mci:
        if not m.plan:
            return m
        moving = {}
        for ld in m.plan.loads:
            if ld.vehicle_id:
                v = await self.ctx.vehicle_repo.get(ld.vehicle_id)
                ld.status = v.status if v and v.assigned_incident_id == m.incident_id else ("planned" if m.status != "approved" else "released")
                if v and ld.status in ("en_route", "transporting", "on_scene"):
                    moving[ld.hospital_id] = moving.get(ld.hospital_id, 0) + len(ld.patient_ids)
        for h in m.plan.hospital_loads:
            h.en_route = moving.get(h.hospital_id, 0)
        return m

    async def approve(self, mci_id: str, reason: str, actor: str) -> Mci:
        ctx = self.ctx
        m = await self.get(mci_id)
        if m.plan is None:
            raise RuleViolation("plan first, then approve")
        if m.status == "approved":
            raise RuleViolation("already approved")
        inc = await ctx.incident_repo.get(m.incident_id)
        now: datetime = ctx.now()
        failed: list[str] = []
        for ld in m.plan.loads:
            if not ld.vehicle_id:
                continue
            try:
                await ctx.vehicles.apply_update(ld.vehicle_id, VehicleStateUpdate(
                    update_id=f"{m.id}-{ld.id}-assign", occurred_at=now, status="assigned", incident_id=m.incident_id,
                    reason=reason, source="reported"), actor)
            except Exception as e:  # noqa: BLE001  (unit changed state since planning: say so, do not guess)
                failed.append(f"{ld.id}: {ld.vehicle_id} could not be assigned ({e})")
                ld.vehicle_id = None
                continue
            a = await ctx.acks.create_and_send(inc, ld.vehicle_id, ld.hospital_id, None, actor)
            ld.assignment_id = a.id
        first = next((ld for ld in m.plan.loads if ld.vehicle_id), None)
        if first:
            await ctx.incidents.assign(m.incident_id, first.vehicle_id, first.hospital_id, actor, f"mass-casualty plan {m.id}: {reason}")
        by_h: dict[str, list[Load]] = {}
        for ld in m.plan.loads:
            by_h.setdefault(ld.hospital_id, []).append(ld)
        inc = await ctx.incident_repo.get(m.incident_id)
        for hid, lds in by_h.items():
            mix: dict[str, int] = {}
            for ld in lds:
                for k, v in ld.triage_mix.items():
                    mix[k] = mix.get(k, 0) + v
            summary = ", ".join(f"{v} {k}" for k, v in sorted(mix.items())) + " planned for this hospital (simulated pre-alert)"
            eta = (min(ld.eta_scene_s or 0 for ld in lds) + lds[0].eta_hospital_s * 0.85, max(ld.eta_scene_s or 0 for ld in lds) + lds[0].eta_hospital_s * 1.25)
            await ctx.cases.upsert_mci(inc, hid, summary, [ld.vehicle_id for ld in lds if ld.vehicle_id], eta, actor)
        plan = m.plan

        def fn(x: Mci) -> Mci:
            x.plan, x.status, x.approved_by, x.approved_at, x.approval_reason = plan, "approved", actor, now, reason
            return x

        m, _ = await self.repo.modify(mci_id, fn)
        await ctx.audit.record(entity_type="mci", entity_id=mci_id, event_type="mci.plan_approved", actor=actor, correlation_id=m.incident_id,
                               source="human_confirmed" if actor != "demo-autopilot" else "simulated", reason=reason,
                               payload={"loads": len(plan.loads), "assigned": sum(1 for ld in plan.loads if ld.vehicle_id), "failed": failed},
                               vis=vis_for(inc))
        m.plan.warnings.extend(failed)
        return m


_ = RuleViolation
