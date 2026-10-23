"""Multi-incident constrained assignment. Each incident gets at most one vehicle and each vehicle
at most one incident; hard feasibility is baked into the cost dict (missing pair = infeasible)."""

from dataclasses import dataclass, field

Cost = dict[tuple[str, str], float]  # (incident_id, vehicle_id) -> cost
SCALE = 1000
UNASSIGNED_PENALTY = 10**9


@dataclass
class Solution:
    assignment: dict[str, str] = field(default_factory=dict)  # incident -> vehicle
    total_cost: float = 0.0
    unassigned: list[str] = field(default_factory=list)
    solver: str = ""
    status: str = ""


def greedy(order: list[str], cost: Cost) -> Solution:
    """Incidents in the given (priority) order each take the cheapest still-free vehicle."""
    used: set[str] = set()
    sol = Solution(solver="greedy", status="ok")
    for inc in order:
        options = sorted(((c, v) for (i, v), c in cost.items() if i == inc and v not in used))
        if not options:
            sol.unassigned.append(inc)
            continue
        c, v = options[0]
        sol.assignment[inc] = v
        sol.total_cost += c
        used.add(v)
    sol.total_cost = round(sol.total_cost, 4)
    return sol


def ortools_assign(incidents: list[str], cost: Cost, time_limit_s: float = 5.0) -> Solution:
    try:
        from ortools.sat.python import cp_model
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("OR-Tools is not installed (pip install ortools)") from e

    m = cp_model.CpModel()
    x = {(i, v): m.NewBoolVar(f"x_{i}_{v}") for (i, v) in sorted(cost)}
    for inc in incidents:
        m.Add(sum(x[k] for k in x if k[0] == inc) <= 1)
    vehicles = sorted({v for _, v in cost})
    for v in vehicles:
        m.Add(sum(x[k] for k in x if k[1] == v) <= 1)
    assigned_terms = [x[k] for k in x]
    # maximise assigned incidents first (big reward), then minimise cost
    m.Minimize(
        sum(int(round(cost[k] * SCALE)) * x[k] for k in x)
        - UNASSIGNED_PENALTY * sum(assigned_terms)
    )
    s = cp_model.CpSolver()
    s.parameters.num_workers = 1
    s.parameters.random_seed = 0
    s.parameters.max_time_in_seconds = time_limit_s
    status = s.Solve(m)
    sol = Solution(solver="ortools_cp_sat", status=s.StatusName(status))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        sol.unassigned = list(incidents)
        return sol
    for (i, v), var in x.items():
        if s.Value(var):
            sol.assignment[i] = v
            sol.total_cost += cost[(i, v)]
    sol.unassigned = [i for i in incidents if i not in sol.assignment]
    sol.total_cost = round(sol.total_cost, 4)
    return sol
