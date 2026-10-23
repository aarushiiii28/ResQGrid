"""The three policies compared in the Replay Lab. They see the world only through `PolicyContext`
(an Observation view plus actions); they cannot read hidden truth such as who will really answer,
whether a hospital has quietly gone down, or what the traffic noise will be.

P0 baseline : nearest eligible unit by straight line, nearest capable hospital. Trusts last-known data.
P1 optimized: travel-time-aware ranking with freshness, coverage and uncertainty (same scoring as the product).
P2 ResQGrid : P1 plus duplicate merging, acknowledgment tracking, a risk monitor and human-approved recovery.
"""

from app.core.capabilities import CATEGORY_HOSPITAL_CAPS, CATEGORY_VEHICLE_TAGS, vehicle_satisfies
from app.core.geo import haversine_m
from app.optimization.scoring import PairInputs, score_pair
from app.routing.geometry import path_hits_circle
from app.schemas.dispatch import Weights
from app.simulation.engine import AssignmentRef, PolicyContext, ReportObs
from app.simulation.model import manhattan_m

WEIGHTS = Weights(w_time=1.0, w_fresh=8.0, w_coverage=60.0, w_uncertainty=1.0, w_switch=8.0, transport_weight=0.5)
Pt = tuple[float, float]


def tags_of(cat: str) -> set[str]:
    return set(CATEGORY_VEHICLE_TAGS[cat])


def caps_of(cat: str) -> set[str]:
    return set(CATEGORY_HOSPITAL_CAPS[cat])


class BasePolicy:
    name = "base"
    version = "base-v0"
    monitor = False
    description = ""

    def bind(self, ctx: PolicyContext) -> None:
        self.ctx = ctx
        self.busy: dict[str, AssignmentRef] = {}
        self.pending: list[ReportObs] = []
        self.seen: list[ReportObs] = []
        self.reps: dict[str, ReportObs] = {}
        self.live: dict[str, AssignmentRef] = {}
        self.dest: dict[str, str] = {}
        self.acked: set[str] = set()
        self.arrived: set[str] = set()
        self.current: dict[str, AssignmentRef] = {}  # report id -> its latest assignment
        self.excluded: dict[str, set[str]] = {}
        self.suspect: dict[str, float] = {}

    # ---- events ------------------------------------------------------------------------------
    def on_call(self, rep: ReportObs) -> None:
        self.reps[rep.id] = rep
        self.ctx.timer(self.ctx.cfg.processing_delay_s, "decide", rep)

    def on_timer(self, name: str, *args) -> None:
        getattr(self, "t_" + name)(*args)

    def on_dispatch(self, ref: AssignmentRef) -> None:
        self.busy[ref.vid] = ref
        self.live[ref.id] = ref
        self.dest[ref.id] = ref.hid
        self.current[ref.report_id] = ref

    def on_ack(self, ref: AssignmentRef) -> None:
        self.acked.add(ref.id)

    def on_arrive(self, ref: AssignmentRef) -> None:
        self.arrived.add(ref.id)
        self.live.pop(ref.id, None)

    def on_unit_free(self, vid: str) -> None:
        self.busy.pop(vid, None)
        self.try_dispatch()

    def on_tick(self) -> None:
        return

    # human fallbacks every dispatcher has: radio the crew, or the caller rings back
    def on_recheck(self, ref: AssignmentRef) -> None:
        self._human_check(ref)

    def on_callback(self, rep: ReportObs) -> None:
        ref = self.current.get(rep.id)
        if ref is not None and ref.id in self.live:
            self._human_check(ref)

    def _human_check(self, ref: AssignmentRef) -> None:
        if ref.id not in self.live or self.current.get(ref.report_id) is not ref:
            return
        acked, responsive = self.ctx.radio_check(ref)
        if acked and responsive:
            return
        self.recover(ref, "no_ack" if not acked else "unresponsive", detected=self.ctx.t)

    # ---- dispatching ---------------------------------------------------------------------------------
    def t_decide(self, rep: ReportObs) -> None:
        if self.is_duplicate(rep):
            return
        self.seen.append(rep)
        self.pending.append(rep)
        self.try_dispatch()

    def is_duplicate(self, rep: ReportObs) -> bool:
        return False

    def order(self) -> list[ReportObs]:
        return sorted(self.pending, key=lambda r: (r.t_call, r.id))

    def try_dispatch(self) -> None:
        for rep in self.order():
            if rep not in self.pending:
                continue
            choice = self.choose(rep, self.excluded.get(rep.id, set()))
            if choice is None:
                self.on_unserved(rep)
                continue
            vid, hid, pred, confirm = choice
            self.pending.remove(rep)
            self.ctx.dispatch(rep.id, vid, hid, pred, "initial", None, confirm)

    def on_unserved(self, rep: ReportObs) -> None:
        return

    def choose(self, rep: ReportObs, excluded: set[str]):
        raise NotImplementedError

    def recover(self, ref: AssignmentRef, kind: str, detected: float, delay: float = 0.0) -> None:
        rep = self.reps[ref.report_id]
        self.ctx.release(ref, f"recovery: {kind}")
        self.live.pop(ref.id, None)
        self.busy.pop(ref.vid, None)
        self.excluded.setdefault(rep.id, set()).add(ref.vid)
        self.suspect[ref.vid] = self.ctx.t + 900
        choice = self.choose(rep, self.excluded[rep.id])
        self.ctx.note_detection(ref, kind, detected, self.ctx.t)
        if choice is None:
            self.pending.append(rep)
            self.ctx.log("recovery_wait", rep.id, f"{ref.vid} released ({kind}); no other unit free yet")
            return
        vid, hid, pred, confirm = choice
        self.ctx.dispatch(rep.id, vid, hid, pred, "redispatch", ref, confirm)


class P0Baseline(BasePolicy):
    name = "P0"
    version = "P0-nearest-eligible-v1"
    description = "Nearest eligible unit by straight line; nearest capable hospital; trusts last-known data; no recovery."

    def choose(self, rep, excluded):
        ctx, cfg = self.ctx, self.ctx.cfg
        need = tags_of(rep.category)
        scene: Pt = (rep.lat, rep.lon)
        cands = [v for v in ctx.view.vehicles() if v.id not in self.busy and v.id not in excluded and vehicle_satisfies(list(v.tags), need)]
        if not cands:
            return None
        v = min(cands, key=lambda x: (haversine_m(scene[0], scene[1], x.lat, x.lon), x.id))
        caps = caps_of(rep.category)
        hs = [h for h in ctx.view.hospitals() if caps <= h.caps]  # capability only: resource state is ignored
        if not hs:
            return None
        h = min(hs, key=lambda x: (haversine_m(scene[0], scene[1], x.lat, x.lon), x.id))
        d = haversine_m(v.lat, v.lon, scene[0], scene[1]) / cfg.speed_mps  # naive: straight line, no roads
        return v.id, h.id, (d * cfg.eta_low, d * cfg.eta_high), False


class P1Optimized(BasePolicy):
    name = "P1"
    version = "P1-ranked-pairs-v1"
    description = "Travel-time-aware ranking with availability and capability constraints, freshness, coverage, uncertainty and entrance confirmation."

    def eta(self, a: Pt, b: Pt) -> float:
        t = manhattan_m(a, b) / self.ctx.cfg.speed_mps
        path = [a, (a[0], b[1]), b]
        for c in self.ctx.view.closures():  # published closures are planned around
            if path_hits_circle(path, (c.lat, c.lon), c.radius):
                return t + max(240.0, 0.6 * t)
        return t

    def is_duplicate(self, rep: ReportObs) -> bool:
        cfg = self.ctx.cfg
        for o in self.seen:
            d = haversine_m(rep.lat, rep.lon, o.lat, o.lon)
            if abs(rep.t_call - o.t_call) <= 900 and d <= 300 and (rep.text_sig == o.text_sig or d < 100) and rep.category == o.category:
                self.ctx.log("duplicate_suggested", rep.id, f"looks like {o.id} ({d:.0f} m, same wording): dispatcher merges after {int(cfg.dup_merge_s)}s, no extra unit sent")
                return True
        return False

    def order(self):
        return sorted(self.pending, key=lambda r: (r.priority, r.t_call, r.id))

    def hospital_options(self, rep: ReportObs, down_ok: bool = False):
        cfg = self.ctx.cfg
        caps = caps_of(rep.category)
        out = []
        for h in self.ctx.view.hospitals():
            if not caps <= h.caps or (caps & h.down):
                continue
            stale = h.age_s > cfg.hospital_stale_s
            out.append((h, 1.0 if stale else 0.0))  # a stale "up" is unknown, not available
        return out

    def choose(self, rep, excluded):
        ctx, cfg = self.ctx, self.ctx.cfg
        now = ctx.t
        need = tags_of(rep.category)
        scene: Pt = (rep.lat, rep.lon)
        vehs = [v for v in ctx.view.vehicles() if v.id not in self.busy and v.id not in excluded and self.suspect.get(v.id, 0) <= now
                and vehicle_satisfies(list(v.tags), need) and v.age_s <= cfg.vehicle_stale_s]
        hosp = self.hospital_options(rep)
        if not vehs or not hosp:
            return None
        vehs.sort(key=lambda v: (manhattan_m((v.lat, v.lon), scene), v.id))
        hosp.sort(key=lambda x: (manhattan_m(scene, (x[0].lat, x[0].lon)), x[0].id))
        idle = {v.id: (v.lat, v.lon) for v in ctx.view.vehicles() if v.id not in self.busy and v.age_s <= cfg.vehicle_stale_s}
        cov_before = ctx.coverage(list(idle.values()))
        best = None
        for v in vehs[:6]:
            cov_after = ctx.coverage([p for k, p in idle.items() if k != v.id])
            ts = self.eta((v.lat, v.lon), scene)
            for h, hfresh in hosp[:4]:
                th = self.eta(scene, (h.lat, h.lon))
                comp = score_pair(PairInputs(
                    scene_mid_s=ts, hospital_mid_s=th, total_low_s=(ts + th) * cfg.eta_low, total_high_s=(ts + th) * cfg.eta_high,
                    vehicle_freshness=min(1.0, v.age_s / cfg.vehicle_stale_s), hospital_freshness=hfresh,
                    coverage_loss=max(0.0, cov_before - cov_after)), WEIGHTS)
                key = (comp.total, v.id, h.id)
                if best is None or key < best[0]:
                    best = (key, v.id, h.id, (ts * cfg.eta_low, ts * cfg.eta_high))
        return best[1], best[2], best[3], True


class P2ResQGrid(P1Optimized):
    name = "P2"
    version = "P2-recovery-ack-v1"
    monitor = True
    description = ("P1 plus acknowledgment tracking, a risk monitor (closures, stale GPS, stale or unavailable hospital data) and "
                   "human-approved recovery with confirmation before action and a priority-gap rule for competing incidents.")

    def __init__(self) -> None:
        self.flagged: set[tuple[str, str]] = set()

    def bind(self, ctx):
        super().bind(ctx)
        self.flagged = set()
        self.preempting: set[str] = set()

    def on_dispatch(self, ref):
        super().on_dispatch(ref)
        self.ctx.timer(self.ctx.cfg.ack_timeout_s, "ack_deadline", ref)

    def t_ack_deadline(self, ref: AssignmentRef) -> None:
        if ref.id in self.acked or ref.id not in self.live or self.current.get(ref.report_id) is not ref:
            return
        self.ctx.log("risk_detected", ref.report_id, f"no acknowledgment from {ref.vid} after {int(self.ctx.cfg.ack_timeout_s)}s: re-dispatch proposed, awaiting approval")
        self.ctx.timer(self.ctx.cfg.approval_delay_s, "approve_recover", ref, "no_ack", self.ctx.t)

    def t_approve_recover(self, ref: AssignmentRef, kind: str, detected: float) -> None:
        if ref.id in self.live and self.current.get(ref.report_id) is ref:
            self.recover(ref, kind, detected)

    # ---- monitor ----------------------------------------------------------------------------------------------
    def on_tick(self) -> None:
        ctx, cfg = self.ctx, self.ctx.cfg
        obs = {v.id: v for v in ctx.view.vehicles()}
        hobs = {h.id: h for h in ctx.view.hospitals()}
        closures = ctx.view.closures()
        for ref in list(self.live.values()):
            if self.current.get(ref.report_id) is not ref:
                continue
            st = ctx.view.crew_status(ref)
            rep = self.reps[ref.report_id]
            v = obs[ref.vid]
            scene: Pt = (rep.lat, rep.lon)
            if st["acked"] and not st["arrived"]:
                path = [(v.lat, v.lon), (v.lat, scene[1]), scene]
                for c in closures:
                    if ("closure", ref.id) not in self.flagged and path_hits_circle(path, (c.lat, c.lon), c.radius):
                        self.flagged.add(("closure", ref.id))
                        ctx.log("risk_detected", rep.id, f"closure {c.id} on the route of {ref.vid}: reroute proposed, awaiting approval")
                        ctx.timer(cfg.approval_delay_s, "approve_reroute", ref, ctx.t)
                if v.age_s > cfg.vehicle_stale_s and ("gps", ref.id) not in self.flagged:
                    self.flagged.add(("gps", ref.id))
                    ctx.log("risk_detected", rep.id, f"{ref.vid} position is {int(v.age_s)}s old: asking the crew to confirm (nothing changes yet)")
                    ctx.timer(cfg.confirm_reply_s, "crew_reply", ref, ctx.t)
            if st["done"]:
                continue
            hid = self.dest[ref.id]
            h = hobs[hid]
            caps = caps_of(rep.category)
            if caps & h.down and ("cap", ref.id, hid) not in self.flagged:
                self.flagged.add(("cap", ref.id, hid))
                ctx.log("risk_detected", rep.id, f"{hid} reports a required resource unavailable: alternate destination proposed, awaiting approval")
                ctx.timer(cfg.approval_delay_s, "approve_redirect", ref, ctx.t)
            elif h.age_s > cfg.hospital_stale_s and ("stale", ref.id, hid) not in self.flagged:
                self.flagged.add(("stale", ref.id, hid))
                ctx.log("risk_detected", rep.id, f"{hid} data is {int(h.age_s)}s old: asking the hospital to confirm (nothing changes yet)")
                ctx.timer(cfg.confirm_reply_s, "hospital_reply", ref, ctx.t)

    def t_approve_reroute(self, ref: AssignmentRef, detected: float) -> None:
        if ref.id in self.live and self.ctx.reroute(ref):
            self.ctx.note_detection(ref, "closure", detected, self.ctx.t)

    def t_crew_reply(self, ref: AssignmentRef, detected: float) -> None:
        if ref.id not in self.live or self.current.get(ref.report_id) is not ref:
            return
        acked, responsive = self.ctx.radio_check(ref)
        if responsive:
            self.ctx.log("risk_cleared", ref.report_id, f"{ref.vid} confirmed: false alarm, plan kept (no reassignment)")
            return
        self.ctx.log("risk_detected", ref.report_id, f"{ref.vid} did not answer: re-dispatch proposed, awaiting approval")
        self.ctx.timer(self.ctx.cfg.approval_delay_s, "approve_recover", ref, "unresponsive", detected)

    def t_hospital_reply(self, ref: AssignmentRef, detected: float) -> None:
        rep = self.reps[ref.report_id]
        hid = self.dest[ref.id]
        if ref.id not in self.live and not self.ctx.view.crew_status(ref)["arrived"]:
            return
        if self.ctx.confirm_hospital(hid, caps_of(rep.category)):
            self.ctx.log("risk_cleared", rep.id, f"{hid} confirmed available: plan kept")
            return
        self.ctx.log("risk_detected", rep.id, f"{hid} confirmed unavailable: alternate destination proposed, awaiting approval")
        self.ctx.timer(self.ctx.cfg.approval_delay_s, "approve_redirect", ref, detected)

    def t_approve_redirect(self, ref: AssignmentRef, detected: float) -> None:
        ctx = self.ctx
        if ctx.view.crew_status(ref)["done"] or ctx.view.crew_status(ref)["cancelled"]:
            return
        rep = self.reps[ref.report_id]
        scene: Pt = (rep.lat, rep.lon)
        opts = [(h, f) for h, f in self.hospital_options(rep) if h.id != self.dest[ref.id]]
        if not opts:
            return
        h = min(opts, key=lambda x: (x[1], manhattan_m(scene, (x[0].lat, x[0].lon)), x[0].id))[0]
        ctx.redirect(ref, h.id)
        self.dest[ref.id] = h.id
        ctx.note_detection(ref, "hospital", detected, ctx.t)

    # ---- competing incidents ------------------------------------------------------------------------------------
    def on_unserved(self, rep: ReportObs) -> None:
        ctx = self.ctx
        if rep.priority != 1 or rep.id in self.preempting:
            return
        need = tags_of(rep.category)
        obs = {v.id: v for v in ctx.view.vehicles()}
        scene: Pt = (rep.lat, rep.lon)
        victims = []
        for ref in self.live.values():
            vr = self.reps[ref.report_id]
            if vr.priority - rep.priority >= 2 and not ctx.view.crew_status(ref)["arrived"] and vehicle_satisfies(list(obs[ref.vid].tags), need):
                victims.append((manhattan_m((obs[ref.vid].lat, obs[ref.vid].lon), scene), ref.id, ref))
        if not victims:
            return
        victim = min(victims)[2]
        self.preempting.add(rep.id)
        ctx.log("risk_detected", rep.id, f"competing incident: no free unit for this P1; {victim.vid} is on a P3 call and not yet there. Hand-over proposed, awaiting approval")
        ctx.timer(ctx.cfg.approval_delay_s, "approve_preempt", rep, victim)

    def t_approve_preempt(self, rep: ReportObs, victim: AssignmentRef) -> None:
        ctx = self.ctx
        self.preempting.discard(rep.id)
        if victim.id not in self.live or ctx.view.crew_status(victim)["arrived"]:
            self.try_dispatch()
            return
        vrep = self.reps[victim.report_id]
        ctx.release(victim, "preempt")
        self.live.pop(victim.id, None)
        self.busy.pop(victim.vid, None)
        self.pending.append(vrep)  # the preempted incident is re-queued, not dropped
        choice = self.choose(rep, self.excluded.get(rep.id, set()))
        if choice is not None and rep in self.pending:
            vid, hid, pred, confirm = choice
            self.pending.remove(rep)
            ctx.dispatch(rep.id, vid, hid, pred, "preempt", victim, confirm)
        self.try_dispatch()


POLICIES = {"P0": P0Baseline, "P1": P1Optimized, "P2": P2ResQGrid}
