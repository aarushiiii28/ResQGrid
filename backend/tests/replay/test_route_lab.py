import copy

from app.services import routing_policy as rp
from app.simulation.route_lab import NAMES, make_scenario, run_compare
from tests.conftest import token_for

POLICY = rp.load_policy()


def test_same_seeds_give_identical_results_and_other_seeds_differ():
    a, b, c = run_compare(1, 4, sweep=False), run_compare(1, 4, sweep=False), run_compare(50, 4, sweep=False)
    assert a == b
    assert a["per_seed"] != c["per_seed"]
    assert make_scenario(3) == make_scenario(3) and make_scenario(3) != make_scenario(4)


def test_all_three_policies_run_on_identical_scenarios_with_every_metric():
    r = run_compare(1, 5, sweep=False)
    assert r["label"].startswith("SIMULATED") and r["policy_version"] == "stable-v1" and r["names"] == NAMES
    for row in r["per_seed"]:
        assert set(row["policies"]) == {"A", "B", "C"}
        for p in "ABC":
            m = row["policies"][p]
            assert m["total_time_s"] > 0 and m["reroutes"] >= 0 and m["eta_std_s"] >= 0 and m["changed_turns"] >= 0
            assert (m["wrong_turn_loss_s"] is None) == (not row["has_wrong_turn"])
    for p in "ABC":
        for k in ("total_time_s", "reroutes", "eta_std_s", "changed_turns", "wrong_turn_loss_s"):
            assert k in r["summary"][p]


def test_never_reroute_never_changes_the_route_and_the_other_two_react_to_the_world():
    r = run_compare(1, 8, sweep=False)
    assert all(row["policies"]["B"]["reroutes"] == 0 and row["policies"]["B"]["changed_turns"] == 0 for row in r["per_seed"])
    assert r["summary"]["A"]["reroutes"]["mean"] > 0 and r["summary"]["C"]["reroutes"]["mean"] > 0
    assert r["summary"]["B"]["total_time_s"]["mean"] > r["summary"]["C"]["total_time_s"]["mean"]       # ignoring blocked roads and wrong turns costs time


def test_the_stable_policy_changes_routes_no_more_often_than_reroute_on_every_change():
    r = run_compare(1, 12, sweep=False)
    assert r["summary"]["C"]["reroutes"]["mean"] <= r["summary"]["A"]["reroutes"]["mean"]
    assert r["summary"]["C"]["changed_turns"]["mean"] <= r["summary"]["A"]["changed_turns"]["mean"]
    assert r["summary"]["C"]["eta_std_s"]["mean"] <= r["summary"]["A"]["eta_std_s"]["mean"]


def test_where_the_stable_policy_is_worse_is_reported_with_counts():
    r = run_compare(1, 12, sweep=False)
    worse = r["where_stable_is_worse"]
    assert worse and all({"vs", "metric", "label", "seeds_worse", "of", "mean_delta"} <= set(w) for w in worse)
    assert all(0 < w["seeds_worse"] <= w["of"] for w in worse)
    assert any(w["vs"] == "B" and w["metric"] == "reroutes" for w in worse)       # it changes the route when never-reroute does not


def test_threshold_sweep_marks_the_current_setting_and_changing_a_threshold_changes_behaviour():
    r = run_compare(1, 6, sweep=True)
    cur = [s for s in r["sweep"] if s["current"]]
    assert {s["setting"] for s in cur} == {"min_saving_s", "sustained_evaluations"} and len(r["sweep"]) == 10
    assert all({"mean_reroutes", "mean_total_time_s", "mean_eta_std_s"} <= set(s) for s in r["sweep"])
    loose = copy.deepcopy(POLICY)
    loose["congestion"].update(eta_increase_pct=1, eta_increase_s=1, sustained_evaluations=1, min_saving_s=0, min_saving_pct=0, cooldown_s=0)
    tight = copy.deepcopy(POLICY)
    tight["congestion"].update(eta_increase_pct=500, eta_increase_s=5000)
    lo, hi = run_compare(1, 8, loose, sweep=False), run_compare(1, 8, tight, sweep=False)
    assert lo["summary"]["C"]["reroutes"]["mean"] >= hi["summary"]["C"]["reroutes"]["mean"]


def test_api_endpoint_runs_the_route_lab_for_dispatchers_only(client):
    r = client.post("/api/simulation/routes", headers=token_for(role="dispatcher"), json={"seed": 2, "n": 3, "sweep": False})
    assert r.status_code == 200 and r.json()["seeds"] == [2, 3, 4] and r.json()["policy_version"] == "stable-v1"
    assert client.post("/api/simulation/routes", headers=token_for("caller-1"), json={}).status_code == 403
    assert client.post("/api/simulation/routes", headers=token_for(role="dispatcher"), json={"n": 1}).status_code == 422
