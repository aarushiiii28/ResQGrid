"""M15 split of mass-casualty patients across hospitals. CP-SAT: every transport patient gets a
hospital slot or is reported as overflow; slots at a hospital get more expensive the fuller it is,
so load spreads out; reds are never displaced by lower triage when capacity is short."""

from dataclasses import dataclass, field

SCALE = 1000
LOAD_PENALTY_MIN = 2.0  # each extra patient at a hospital costs this many minutes-equivalent
OVERFLOW = {"red": 10**9, "unassigned": 10**9, "amber": 10**8, "green": 10**7}


@dataclass
class HospitalCap:
    id: str
    er: int  # patients it can take now (fresh, available data only)
    icu: int  # of which red
    eta_min: float
    accepts: set[str] = field(default_factory=lambda: {"red", "amber", "green", "unassigned"})


@dataclass
class SplitResult:
    assignment: dict[str, str] = field(default_factory=dict)  # patient -> hospital
    overflow: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    solver: str = ""
    status: str = ""


def greedy_split(patients: list[tuple[str, str]], hospitals: list[HospitalCap]) -> SplitResult:
    """Baseline: reds first, each to the nearest eligible hospital that still has room."""
    er = {h.id: h.er for h in hospitals}
    icu = {h.id: h.icu for h in hospitals}
    res = SplitResult(solver="greedy_nearest", status="ok")
    order = sorted(patients, key=lambda p: ({"red": 0, "unassigned": 0, "amber": 1, "green": 2}[p[1]], p[0]))
    for pid, tri in order:
        opts = sorted((h for h in hospitals if tri in h.accepts and er[h.id] > 0 and (tri not in ("red", "unassigned") or icu[h.id] > 0)),
                      key=lambda h: (h.eta_min, h.id))
        if not opts:
            res.overflow.append(pid)
            continue
        h = opts[0]
        res.assignment[pid] = h.id
        res.total_cost += h.eta_min + LOAD_PENALTY_MIN * (h.er - er[h.id])  # same cost model as the optimiser
        er[h.id] -= 1
        if tri in ("red", "unassigned"):
            icu[h.id] -= 1
    res.total_cost = round(res.total_cost, 3)
    return res


def solve_split(patients: list[tuple[str, str]], hospitals: list[HospitalCap], time_limit_s: float = 5.0) -> SplitResult:
    from ortools.sat.python import cp_model

    m = cp_model.CpModel()
    y: dict[tuple[str, str, int], object] = {}
    for pid, tri in patients:
        for h in hospitals:
            if tri not in h.accepts:
                continue
            for k in range(h.er):
                y[(pid, h.id, k)] = m.NewBoolVar(f"y_{pid}_{h.id}_{k}")
    for pid, _ in patients:
        m.Add(sum(v for (p, _, _), v in y.items() if p == pid) <= 1)
    for h in hospitals:
        for k in range(h.er):
            m.Add(sum(v for (_, hh, kk), v in y.items() if hh == h.id and kk == k) <= 1)
        reds = [v for (p, hh, _), v in y.items() if hh == h.id and dict(patients)[p] in ("red", "unassigned")]
        if reds:
            m.Add(sum(reds) <= h.icu)
    eta = {h.id: h.eta_min for h in hospitals}
    obj = []
    tri_of = dict(patients)
    for (p, h, k), v in y.items():
        obj.append(int(round((eta[h] + LOAD_PENALTY_MIN * k) * SCALE)) * v)
        obj.append(-OVERFLOW[tri_of[p]] * v)
    m.Minimize(sum(obj))
    s = cp_model.CpSolver()
    s.parameters.num_workers = 1
    s.parameters.random_seed = 0
    s.parameters.max_time_in_seconds = time_limit_s
    status = s.Solve(m)
    res = SplitResult(solver="ortools_cp_sat", status=s.StatusName(status))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        res.overflow = [p for p, _ in patients]
        return res
    for (p, h, k), v in y.items():
        if s.Value(v):
            res.assignment[p] = h
            res.total_cost += eta[h] + LOAD_PENALTY_MIN * k
    res.overflow = [p for p, _ in patients if p not in res.assignment]
    res.total_cost = round(res.total_cost, 3)
    return res
