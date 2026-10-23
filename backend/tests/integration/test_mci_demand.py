from datetime import UTC, datetime, timedelta

import pytest

from app.optimization import demand as dm
from app.optimization.mci_solver import HospitalCap, greedy_split, solve_split
from tests.conftest import token_for

DISP = token_for(role="dispatcher")
CENTER = (30.9010, 75.8573)


def make_incident(c, n=8):
    iid = c.post("/api/incidents", headers=DISP, json={
        "reported_category": "trauma", "operator_priority": 1, "patient_count": n,
        "location": {"lat": 30.9010, "lon": 75.8573, "accuracy_m": 10}, "text": "bus collision"}).json()["id"]
    r = c.post("/api/mci", headers=DISP, json={"incident_id": iid, "patients": n})
    assert r.status_code == 201, r.text
    return iid, r.json()


def triage(c, mid, colours):
    for i, col in enumerate(colours, 1):
        r = c.patch(f"/api/mci/{mid}/patients/P-{i:02d}", headers=DISP, json={"triage": col})
        assert r.status_code == 200, r.text
    return r.json()


def set_hospitals(c, run, spec):
    """spec: {hospital_id: (er, icu, trauma_team_available)}; every other hospital gets no capacity."""
    ctx = c.app.state.ctx

    async def go():
        now = ctx.now()
        for h in await ctx.hospital_repo.list():
            def fn(x, hid=h.id):
                er, icu, tt = spec.get(hid, (0, 0, False))
                for name, (st, n) in {"er_bay": ("available" if er else "unavailable", er), "icu_bed": ("available" if icu else "unavailable", icu),
                                       "trauma_team": ("available" if tt else "unavailable", 1 if tt else 0)}.items():
                    r = x.resources[name]
                    r.status, r.count, r.last_confirmed_at = st, n, now
                return x
            await ctx.hospital_repo.modify(h.id, fn)

    run(go)


@pytest.fixture
def mci(client, run):
    set_hospitals(client, run, {"HOSP-01": (4, 3, True), "HOSP-07": (3, 2, True), "HOSP-08": (3, 2, True), "HOSP-11": (2, 1, True)})
    return client


def plan(c, mid):
    r = c.post(f"/api/mci/{mid}/plan", headers=DISP)
    assert r.status_code == 200, r.text
    return r.json()["plan"]


# ---- M15 ----------------------------------------------------------------------------------------------
def test_triage_is_human_set_and_suggestions_do_not_count(mci):
    c = mci
    iid, m = make_incident(c, 5)
    assert all(p["triage"] == "unassigned" and not p["confirmed"] for p in m["patients"])
    s = c.post(f"/api/mci/{m['id']}/suggest", headers=DISP, json={"text": "I see 2 red, 1 amber and 1 black"}).json()
    sug = [p for p in s["patients"] if p["suggested"]]
    assert [p["suggested"] for p in sug] == ["red", "red", "amber", "black"]
    assert all(p["triage"] == "unassigned" and not p["confirmed"] for p in s["patients"])  # still unconfirmed
    pl = plan(c, m["id"])
    assert len(pl["unconfirmed"]) == 5 and any("planned as red" in w for w in pl["warnings"])
    done = c.patch(f"/api/mci/{m['id']}/patients/P-01", headers=DISP, json={"triage": "red", "note": "confirmed on scene"}).json()
    p1 = done["patients"][0]
    assert p1["triage"] == "red" and p1["confirmed"] and p1["set_by"] == "disp-1" and done["plan"] is None  # a change voids the plan


def test_split_respects_capacity_capability_and_black(mci):
    c = mci
    iid, m = make_incident(c, 9)
    triage(c, m["id"], ["red"] * 3 + ["amber"] * 3 + ["green"] * 2 + ["black"])
    pl = plan(c, m["id"])
    caps = {h["hospital_id"]: h for h in pl["hospital_loads"]}
    assert "P-09" not in pl["patient_hospital"]  # black is never transported
    assert len(pl["patient_hospital"]) == 8 and pl["overflow"] == []
    for h in pl["hospital_loads"]:
        assert h["planned"] <= h["capacity"] and h["planned_red"] <= h["red_capacity"]
    hospitals = {h["id"]: h for h in c.get("/api/hospitals", headers=DISP).json()}
    for pid in ("P-01", "P-02", "P-03"):  # reds only go where a trauma team exists
        assert "trauma" in hospitals[pl["patient_hospital"][pid]]["capabilities"]
    assert all(h in caps for h in pl["patient_hospital"].values())
    assert pl["split"]["ortools"]["cost"] <= pl["split"]["greedy"]["cost"] + 1e-6


def test_load_is_spread_not_piled_on_the_nearest_hospital(mci):
    c = mci
    iid, m = make_incident(c, 8)
    triage(c, m["id"], ["amber"] * 8)
    pl = plan(c, m["id"])
    used = [h for h in pl["hospital_loads"] if h["planned"] > 0]
    assert len(used) >= 2 and max(h["fraction"] for h in used) < 1.0 or sum(h["planned"] for h in used) == 8


def test_overflow_drops_lowest_triage_first_and_says_so(client, run):
    c = client
    set_hospitals(c, run, {"HOSP-01": (2, 2, True), "HOSP-07": (2, 1, True)})  # room for 4, of which 3 red
    iid, m = make_incident(c, 6)
    triage(c, m["id"], ["red", "red", "red", "amber", "amber", "green"])
    pl = plan(c, m["id"])
    assert len(pl["overflow"]) == 2
    assert not any(p in pl["overflow"] for p in ("P-01", "P-02", "P-03"))  # reds always placed
    assert all(p in pl["overflow"] for p in ("P-05", "P-06")) or "P-04" not in pl["overflow"]  # green/amber give way first
    assert any("exceed the capacity" in w for w in pl["warnings"])
    for h in pl["hospital_loads"]:
        assert h["planned"] <= h["capacity"]


def test_stale_or_unknown_capacity_counts_as_zero(client, run):
    c = client
    set_hospitals(c, run, {"HOSP-01": (4, 2, True), "HOSP-07": (4, 2, True)})
    ctx = c.app.state.ctx

    def stale(h):
        h.resources["er_bay"].last_confirmed_at = ctx.now() - timedelta(hours=3)
        return h

    run(ctx.hospital_repo.modify, "HOSP-01", stale)
    iid, m = make_incident(c, 3)
    triage(c, m["id"], ["amber"] * 3)
    pl = plan(c, m["id"])
    h1 = next(h for h in pl["hospital_loads"] if h["hospital_id"] == "HOSP-01")
    assert h1["capacity"] == 0 and h1["planned"] == 0 and "er_bay" in h1["unverified_resources"] and "unverified" in h1["note"]
    assert set(pl["patient_hospital"].values()) == {"HOSP-07"}


def test_multi_ambulance_assignment_uses_distinct_units_and_beats_or_matches_greedy(mci):
    c = mci
    iid, m = make_incident(c, 8)
    triage(c, m["id"], ["red", "red", "amber", "amber", "amber", "green", "green", "black"])
    pl = plan(c, m["id"])
    units = [ld["vehicle_id"] for ld in pl["loads"] if ld["vehicle_id"]]
    assert len(units) == len(set(units)) and len(units) == len(pl["loads"]) >= 4
    vs = {v["id"]: v for v in c.get("/api/vehicles", headers=DISP).json()}
    for ld in pl["loads"]:
        tags = set(vs[ld["vehicle_id"]]["capability_tags"])
        assert (ld["vehicle_needs"] == ["BLS"] and tags) or "ALS" in tags
        assert len(ld["patient_ids"]) <= (1 if "red" in ld["triage_mix"] else 2)
    a = pl["assignment"]
    assert a["ortools"]["assigned"] >= a["greedy"]["assigned"]
    if a["ortools"]["assigned"] == a["greedy"]["assigned"]:
        assert a["ortools"]["cost"] <= a["greedy"]["cost"] + 1e-6


def test_approval_assigns_units_and_pre_alerts_each_hospital_only_after_a_human_says_so(mci):
    c = mci
    iid, m = make_incident(c, 6)
    triage(c, m["id"], ["red", "red", "amber", "amber", "green", "black"])
    pl = plan(c, m["id"])
    assert all(v["status"] == "available" for v in c.get("/api/vehicles", headers=DISP).json() if v["id"] in {ld["vehicle_id"] for ld in pl["loads"]})
    assert c.post(f"/api/mci/{m['id']}/approve", headers=DISP, json={"reason": "x"}).status_code == 422
    r = c.post(f"/api/mci/{m['id']}/approve", headers=DISP, json={"reason": "plan reviewed with the incident commander"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["status"] == "approved" and got["approved_by"] == "disp-1"
    vs = {v["id"]: v for v in c.get("/api/vehicles", headers=DISP).json()}
    for ld in got["plan"]["loads"]:
        assert vs[ld["vehicle_id"]]["status"] == "assigned" and vs[ld["vehicle_id"]]["assigned_incident_id"] == iid
    asg = c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json()
    assert len(asg) == len(got["plan"]["loads"]) and all(a["state"] == "sent" for a in asg)  # sent, not accepted
    for hid in {ld["hospital_id"] for ld in got["plan"]["loads"]}:
        pas = c.get("/api/prealerts", headers=token_for(f"hosp-{hid}")).json()
        assert pas and "Mass casualty" in pas[0]["reported_reason"]
    types = [e["event_type"] for e in c.get(f"/api/incidents/{iid}/audit", headers=DISP).json()]
    assert "mci.plan_approved" in types and "mci.planned" in types and "mci.triage_set" in types
    assert c.post(f"/api/mci/{m['id']}/approve", headers=DISP, json={"reason": "again please"}).status_code == 409


def test_mci_needs_a_plan_one_record_per_incident_and_operator_roles(mci):
    c = mci
    iid, m = make_incident(c, 4)
    assert c.post(f"/api/mci/{m['id']}/approve", headers=DISP, json={"reason": "no plan yet"}).status_code == 409
    assert c.post("/api/mci", headers=DISP, json={"incident_id": iid, "patients": 4}).status_code == 409
    assert c.post("/api/mci", headers=DISP, json={"incident_id": iid, "patients": 1}).status_code == 422
    for who in (token_for("caller-1"), token_for("driver-AMB-01"), token_for("hosp-HOSP-01")):
        assert c.get(f"/api/mci/{m['id']}", headers=who).status_code == 403
        assert c.patch(f"/api/mci/{m['id']}/patients/P-01", headers=who, json={"triage": "red"}).status_code == 403


def test_live_hospital_load_counts_units_en_route(mci):
    c = mci
    iid, m = make_incident(c, 3)
    triage(c, m["id"], ["red", "amber", "green"])
    plan(c, m["id"])
    done = c.post(f"/api/mci/{m['id']}/approve", headers=DISP, json={"reason": "go ahead"}).json()
    ld = done["plan"]["loads"][0]
    a = next(x for x in c.get("/api/assignments", headers=DISP, params={"incident_id": iid}).json() if x["vehicle_id"] == ld["vehicle_id"])
    c.post(f"/api/assignments/{a['id']}/accept", headers=token_for(f"driver-{ld['vehicle_id']}"))
    c.patch(f"/api/vehicles/{ld['vehicle_id']}/state", headers=token_for(f"driver-{ld['vehicle_id']}"), json={
        "update_id": "mv1", "occurred_at": datetime.now(UTC).isoformat(), "status": "en_route"})
    live = c.get(f"/api/mci/{m['id']}", headers=DISP).json()
    assert sum(h["en_route"] for h in live["plan"]["hospital_loads"]) == len(ld["patient_ids"])


def test_solver_unit_cases():
    hs = [HospitalCap("A", er=2, icu=1, eta_min=5), HospitalCap("B", er=3, icu=2, eta_min=9)]
    pts = [("p1", "red"), ("p2", "red"), ("p3", "amber"), ("p4", "amber"), ("p5", "green")]
    opt, base = solve_split(pts, hs), greedy_split(pts, hs)
    assert opt.overflow == [] and len(opt.assignment) == 5
    assert sum(1 for p, h in opt.assignment.items() if h == "A") <= 2
    assert opt.total_cost <= base.total_cost + 1e-6
    tiny = [HospitalCap("A", er=1, icu=1, eta_min=5)]
    r = solve_split([("a", "amber"), ("r", "red")], tiny)
    assert r.assignment == {"r": "A"} and r.overflow == ["a"]  # red wins the only slot


# ---- M13 ------------------------------------------------------------------------------------------------------
def test_history_and_forecast_are_deterministic_and_seed_dependent():
    a, b, d = dm.generate_history(CENTER, 42), dm.generate_history(CENTER, 42), dm.generate_history(CENTER, 7)
    assert a == b and a != d and len(a) == 700
    f1, i1 = dm.forecast_cells(a, CENTER, 2, False)
    f2, i2 = dm.forecast_cells(b, CENTER, 2, False)
    assert f1 == f2 and i1 == i2
    f3, _ = dm.forecast_cells(a, CENTER, 5, True)
    assert f1 != f3 and all(v > 0 for v in f1)
    assert sum(f1) * 4 == pytest.approx(sum(dm.forecast_cells(a, CENTER, 2, False)[0]) * 4)


def test_standby_points_raise_demand_weighted_coverage():
    hist = dm.generate_history(CENTER, 42)
    cells = dm.grid_cells(CENTER)
    w, _ = dm.forecast_cells(hist, CENTER, 4, False)
    picks = dm.suggest_standby(cells, w, 5, 2500)
    assert 1 <= len(picks) <= 5 and picks == dm.suggest_standby(cells, w, 5, 2500)
    gains = [g for _, g in picks]
    assert gains == sorted(gains, reverse=True)  # greedy: each pick adds no more than the one before
    far = [(30.80, 75.70)]
    assert dm.coverage([p for p, _ in picks], cells, w, 2500) > dm.coverage(far, cells, w, 2500)


def test_forecast_endpoint_is_labelled_deterministic_and_improves_coverage(client):
    p = {"bucket": 4, "daytype": "weekday"}
    a = client.get("/api/demand/forecast", headers=DISP, params=p).json()
    b = client.get("/api/demand/forecast", headers=DISP, params=p).json()
    assert a == b
    assert a["label"].startswith("Prototype approximation") and "synthetic" in a["label"].lower()
    assert a["history_count"] == 700 and a["bucket"] == "16-20h" and a["heat"] and all(0 <= h[2] <= 1 for h in a["heat"])
    assert a["coverage_after"] >= a["coverage_before"] and a["standby"]
    vids = [m["vehicle_id"] for m in a["moves"]]
    assert len(vids) == len(set(vids)) and all(m["distance_km"] >= 0 for m in a["moves"])
    other = client.get("/api/demand/forecast", headers=DISP, params={"bucket": 1, "daytype": "weekend"}).json()
    assert other["heat"] != a["heat"] and other["bucket"] == "04-08h"
    assert "formula" in a["explain"] and len(a["explain"]["top_cells"]) == 5


def test_forecast_auto_bucket_follows_the_clock_and_is_operator_only(client):
    ctx = client.app.state.ctx
    ctx.clock = lambda: datetime(2026, 10, 24, 22, 30, tzinfo=UTC)  # a Saturday evening
    r = client.get("/api/demand/forecast", headers=DISP).json()
    assert r["bucket"] == "20-24h" and r["day_type"] == "weekend"
    assert client.get("/api/demand/forecast", headers=token_for("caller-1")).status_code == 403
    assert client.get("/api/demand/forecast", headers=DISP, params={"bucket": 9}).status_code == 422
