import copy
import csv
import io
import json
import random

import pytest

from app.simulation.engine import Sim, last_report_time
from app.simulation.model import ALL_FAULTS, Closure, build_world
from app.simulation.policies import POLICIES
from app.simulation.runner import benchmark, make_config, run_compare, run_one
from tests.conftest import token_for

DISP = token_for(role="dispatcher")


def strip(res):
    """Everything a run produced, minus nothing: determinism must hold for the full output."""
    return json.dumps(res, sort_keys=True, default=str)


# ---- determinism ---------------------------------------------------------------------------------------
def test_same_seed_gives_identical_events_and_metrics():
    a, b = run_compare(7), run_compare(7)
    assert strip(a) == strip(b)  # world, timelines, decisions, tracks and metrics, byte for byte
    for p in ("P0", "P1", "P2"):
        assert a["results"][p]["timeline"] and a["results"][p]["metrics"]["incidents"] > 0


def test_different_seeds_give_different_worlds_and_results():
    a, b = run_compare(7), run_compare(8)
    assert a["world"] != b["world"]
    assert a["results"]["P0"]["metrics"] != b["results"]["P0"]["metrics"]


def test_policies_are_compared_on_identical_worlds():
    out = run_compare(11)
    calls = {p: [(e["t"], e["incident"], e["detail"]) for e in r["timeline"] if e["kind"] == "call"] for p, r in out["results"].items()}
    assert calls["P0"] == calls["P1"] == calls["P2"] and calls["P0"]  # same calls at the same times
    inc = {p: [(i["incident"], i["category"], i["priority"], i["t_call"]) for i in r["incidents"]] for p, r in out["results"].items()}
    assert inc["P0"] == inc["P1"] == inc["P2"]
    cfg = make_config(11)
    w1, w2 = build_world(cfg), build_world(cfg)
    assert w1.reports == w2.reports and w1.closures == w2.closures
    # running one policy first does not change what another sees
    solo = run_one(cfg, "P2", with_tracks=False)
    assert strip(solo["metrics"]) == strip(out["results"]["P2"]["metrics"])


def test_unknown_fault_or_policy_is_rejected():
    with pytest.raises(ValueError):
        make_config(1, ["asteroid"])


# ---- no future leakage ----------------------------------------------------------------------------------
@pytest.mark.parametrize("pol", ["P0", "P1", "P2"])
def test_future_events_do_not_change_earlier_decisions(pol):
    cfg = make_config(5)
    base = build_world(cfg)
    T = 3000.0
    alt = copy.deepcopy(base)
    # a new closure and a new hospital outage that only begin after T, plus a unit that goes dark after T
    alt.closures.append(Closure("XF", base.reports[0].lat, base.reports[0].lon, 1500.0, T + 200, T + 2400))
    for h in alt.hospitals:
        for c in h.caps:
            h.windows.setdefault(c, []).append((T + 300, T + 2500))
    alt.vehicles[0].offline_at = T + 100
    r1 = Sim(base, POLICIES[pol]())
    r1.run()
    r2 = Sim(alt, POLICIES[pol]())
    r2.run()
    before1 = [d for d in r1.decisions if d[0] < T + 60]
    before2 = [d for d in r2.decisions if d[0] < T + 60]
    assert before1 and before1 == before2  # identical choices up to the moment the new facts could be known


def test_observations_never_look_ahead_and_ages_are_non_negative():
    cfg = make_config(3)
    w = build_world(cfg)
    rng = random.Random(1)
    for v in w.vehicles:
        for _ in range(200):
            t = rng.uniform(0, 9000)
            r = last_report_time(v.phase, cfg.gps_report_s, t, v.gps_gaps, v.offline_at)
            assert r <= t + 1e-9
            if v.offline_at is not None:
                assert r <= v.offline_at + 1e-9
            for a, b in v.gps_gaps:
                assert not (a <= r < b)  # no report is ever produced inside an outage
    for h in w.hospitals:
        for _ in range(200):
            t = rng.uniform(0, 9000)
            r = last_report_time(h.phase, cfg.hospital_report_s, t, [], h.stops_reporting_at)
            assert r <= t + 1e-9 and (h.stops_reporting_at is None or r <= h.stops_reporting_at + 1e-9)


def test_policies_have_no_access_to_hidden_truth():
    ctx_attrs = {a for a in dir(Sim(build_world(make_config(1)), POLICIES["P0"]()).ctx) if not a.startswith("_")}
    assert not ({"veh", "w", "assignments", "heap", "hosp", "inc", "by_id", "reports"} & ctx_attrs)


# ---- faults really change the world and the policies react to what they can see -------------------------
def first_seed(fault, pred, limit=40):
    for s in range(1, limit):
        out = run_compare(s, [fault])
        if pred(out):
            return s, out
    pytest.fail(f"no seed in 1..{limit} showed the effect of {fault}")


def kinds(res):
    return {e["kind"] for e in res["timeline"]}


def test_duplicate_calls_waste_baseline_units_but_are_merged_by_p1_and_p2():
    s, out = first_seed("duplicate_calls", lambda o: o["results"]["P0"]["metrics"]["wasted_dispatches"] > 0)
    r = out["results"]
    assert r["P1"]["metrics"]["wasted_dispatches"] == 0 and r["P2"]["metrics"]["wasted_dispatches"] == 0
    assert "duplicate_suggested" in kinds(r["P1"]) and "wasted" in kinds(r["P0"])
    assert any(i["duplicate_of"] for i in out["world"]["incidents"])


def test_no_acknowledgment_p2_detects_in_one_timeout_while_baseline_waits_for_a_human():
    s, out = first_seed("no_acknowledgment", lambda o: o["results"]["P0"]["metrics"]["failures"] > 0)
    for p in ("P0", "P1", "P2"):
        assert out["results"][p]["metrics"]["failures"] >= 1
    p2 = [f for f in out["results"]["P2"]["failures"] if f["kind"] == "no_ack"]
    p0 = [f for f in out["results"]["P0"]["failures"] if f["kind"] == "no_ack"]
    assert p2 and all(f["detected_s"] == 60.0 for f in p2)  # the 60 s watchdog
    assert all(f["detected_s"] is None or f["detected_s"] >= 400 for f in p0)  # dispatcher radio check at 420 s
    assert "risk_detected" in kinds(out["results"]["P2"]) and "risk_detected" not in kinds(out["results"]["P0"])


def test_route_closure_is_rerouted_by_p2_only_and_only_before_the_unit_crosses_it():
    s, out = first_seed("route_closure", lambda o: "reroute" in kinds(o["results"]["P2"]))
    assert "reroute" not in kinds(out["results"]["P0"]) and "reroute" not in kinds(out["results"]["P1"])
    det = [f for f in out["results"]["P2"]["failures"] if f["kind"] == "closure" and f["detected_s"] is not None]
    assert det and all(f["decided_s"] >= f["detected_s"] for f in det)


def test_blocked_entrance_delays_only_units_that_did_not_get_confirmation_in_time():
    s, out = first_seed("blocked_entrance", lambda o: "entrance_blocked" in kinds(o["results"]["P0"]))
    assert any(i["blocked_entrance"] for i in out["world"]["incidents"])
    n = {p: sum(1 for e in r["timeline"] if e["kind"] == "entrance_blocked") for p, r in out["results"].items()}
    assert n["P0"] >= n["P1"] and n["P0"] >= n["P2"]


def test_stale_hospital_data_causes_baseline_diversions_and_p2_redirects():
    s, out = first_seed("stale_hospital_data", lambda o: o["results"]["P0"]["metrics"]["diversions"] > 0 and "redirect" in kinds(o["results"]["P2"]))
    assert any(h["stops_reporting_at"] is not None for h in out["world"]["hospitals"])
    assert "diverted" in kinds(out["results"]["P0"])


def test_stale_gps_false_alarms_do_not_cause_unnecessary_reassignments_in_p2():
    for s in range(1, 25):
        out = run_compare(s, ["stale_gps"])
        assert out["results"]["P2"]["metrics"]["unnecessary_reassignments"] == 0  # confirm first, then act
    cleared = any("risk_cleared" in kinds(run_compare(s, ["stale_gps"])["results"]["P2"]) for s in range(1, 25))
    assert cleared  # at least one false alarm was raised and cleared without moving anyone


def test_offline_unit_is_recovered_by_p2_faster_than_by_a_human_callback():
    s, out = first_seed("offline_unit", lambda o: any(f["kind"] == "unresponsive" for f in o["results"]["P2"]["failures"]))
    p2 = [f for f in out["results"]["P2"]["failures"] if f["kind"] == "unresponsive" and f["detected_s"] is not None]
    assert p2 and max(f["detected_s"] for f in p2) < 900


def test_competing_incident_burst_exists_and_p2_hands_units_over_only_by_priority_gap():
    out = run_compare(3, ["competing_incident"])
    burst = [i for i in out["world"]["incidents"] if i["burst"]]
    assert sorted(i["priority"] for i in burst) == [1, 2, 3, 3]
    for e in out["results"]["P2"]["timeline"]:
        if e["kind"] == "release" and "preempt" in e["detail"]:
            assert any(x["kind"] == "risk_detected" and "competing incident" in x["detail"] for x in out["results"]["P2"]["timeline"])


def test_without_faults_everything_is_served_and_p1_p2_never_waste_or_reassign():
    for s in range(1, 13):
        out = run_compare(s, [])
        for p, r in out["results"].items():
            m = r["metrics"]
            assert m["unserved"] == 0
            # ordinary hospital capacity churn can still divert a patient; nothing else may fail without injected faults
            assert {f["kind"] for f in r["failures"]} <= {"hospital"}
            if p != "P0":
                assert m["wasted_dispatches"] == 0 and m["unnecessary_reassignments"] == 0 and m["oscillations"] == 0


# ---- metrics and distributions ------------------------------------------------------------------------------------
def test_metric_fields_and_basic_sanity():
    m = run_compare(2)["results"]["P2"]["metrics"]
    for k in ("response_median_s", "response_p90_s", "dest_median_s", "pct_meeting_requirements", "late_acks", "missed_acks",
              "dispatch_to_accept_median_s", "arrival_to_handover_median_s", "unnecessary_reassignments", "oscillations",
              "coverage_mean", "eta_calibration", "detect_median_s", "decide_median_s"):
        assert k in m
    assert m["response_p90_s"] >= m["response_median_s"] > 0
    assert 0 <= m["coverage_mean"] <= 1 and 0 <= m["eta_calibration"] <= 1 and 0 <= m["pct_meeting_requirements"] <= 100
    assert m["detect_median_s"] is None or m["decide_median_s"] >= m["detect_median_s"]


def test_benchmark_reports_distributions_pairs_and_where_p2_is_worse():
    b = benchmark(list(range(1, 7)))
    assert b["label"].startswith("SIMULATED")
    d = b["distributions"]["P2"]["response_median_s"]
    assert d["n"] == 6 and d["p10"] <= d["median"] <= d["p90"] and len(d["values"]) == 6
    assert set(b["paired"]) == {"P1_vs_P0", "P2_vs_P0", "P2_vs_P1"}
    c = b["paired"]["P2_vs_P0"]["response_median_s"]
    assert c["better"] + c["worse"] + c["tied"] == 6
    assert isinstance(b["where_p2_is_worse_than_p0"], list)
    assert b == benchmark(list(range(1, 7)))  # deterministic


def test_all_fault_names_are_covered():
    assert set(ALL_FAULTS) == {"duplicate_calls", "stale_hospital_data", "blocked_entrance", "route_closure", "no_acknowledgment",
                               "stale_gps", "offline_unit", "competing_incident"}


# ---- API -----------------------------------------------------------------------------------------------------------------------
def test_api_saves_runs_with_seed_config_versions_timeline_and_metrics(client):
    r = client.post("/api/simulation/runs", headers=DISP, json={"seed": 5})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["id"].startswith("SIM-") and run["seed"] == 5 and run["label"].startswith("SIMULATED")
    assert set(run["results"]) == {"P0", "P1", "P2"} and run["config"]["seed"] == 5 and set(run["faults"]) == set(ALL_FAULTS)
    assert run["policy_versions"]["P2"] == "P2-recovery-ack-v1"
    assert all(run["results"][p]["timeline"] and run["results"][p]["metrics"] and run["results"][p]["tracks"] for p in run["results"])
    got = client.get(f"/api/simulation/runs/{run['id']}", headers=DISP).json()
    assert got == run
    assert any(x["id"] == run["id"] for x in client.get("/api/simulation/runs", headers=DISP).json())
    assert "lives" not in json.dumps(run).lower() or "no lives-saved" in json.dumps(run).lower()


def test_api_same_seed_gives_same_metrics_each_time(client):
    a = client.post("/api/simulation/runs", headers=DISP, json={"seed": 9, "faults": ["route_closure"]}).json()
    b = client.post("/api/simulation/runs", headers=DISP, json={"seed": 9, "faults": ["route_closure"]}).json()
    assert a["id"] != b["id"]
    for p in a["results"]:
        assert a["results"][p]["metrics"] == b["results"][p]["metrics"]
        assert a["results"][p]["timeline"] == b["results"][p]["timeline"]


def test_api_exports_json_and_csv_parts(client):
    run = client.post("/api/simulation/runs", headers=DISP, json={"seed": 4, "policies": ["P0", "P2"]}).json()
    j = client.get(f"/api/simulation/runs/{run['id']}/export", headers=DISP, params={"format": "json"})
    assert json.loads(j.text)["seed"] == 4 and "attachment" in j.headers["content-disposition"]
    for part, col in (("metrics", "metric"), ("incidents", "incident"), ("timeline", "kind")):
        c = client.get(f"/api/simulation/runs/{run['id']}/export", headers=DISP, params={"format": "csv", "part": part})
        rows = list(csv.reader(io.StringIO(c.text)))
        assert rows[0][0].startswith("# SIMULATED") and col in rows[1] and len(rows) > 5
        assert {r[0] for r in rows[2:]} == {"P0", "P2"}


def test_api_validation_and_authorization(client):
    assert client.post("/api/simulation/runs", headers=DISP, json={"seed": 1, "policies": ["P9"]}).status_code == 422
    assert client.post("/api/simulation/runs", headers=DISP, json={"seed": 1, "faults": ["asteroid"]}).status_code == 422
    assert client.post("/api/simulation/runs", headers=DISP, json={"seed": -5}).status_code == 422
    assert client.get("/api/simulation/runs/SIM-9999", headers=DISP).status_code == 404
    for who in (token_for("caller-1"), token_for("driver-AMB-01"), token_for("hosp-HOSP-01")):
        assert client.post("/api/simulation/runs", headers=who, json={"seed": 1}).status_code == 403
        assert client.get("/api/simulation/runs", headers=who).status_code == 403
    assert client.post("/api/simulation/runs", json={"seed": 1}).status_code == 401


def test_api_benchmark(client):
    r = client.post("/api/simulation/benchmark", headers=DISP, json={"seeds": 4})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["seeds"] == [1, 2, 3, 4] and "P2_vs_P0" in b["paired"]
    assert client.post("/api/simulation/benchmark", headers=DISP, json={"seeds": 1}).status_code == 422
    assert client.get("/api/simulation/faults", headers=DISP).json()["faults"] == list(ALL_FAULTS)
