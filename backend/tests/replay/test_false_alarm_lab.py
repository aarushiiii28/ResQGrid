from app.simulation.false_alarm_lab import run_mix
from tests.conftest import token_for


def test_same_seed_gives_identical_results_and_other_seed_differs():
    a, b, c = run_mix(42, 200), run_mix(42, 200), run_mix(43, 200)
    assert a == b
    assert a["metrics"] != c["metrics"]


def test_missed_genuine_in_low_lane_is_reported_and_labelled_simulated():
    r = run_mix(42, 300)
    m = r["metrics"]
    for k in ("false_alarms_flagged", "missed_genuine_in_low", "missed_genuine_in_low_pct", "time_to_verify_median_s",
              "dispatcher_reviews_per_100", "delayed_genuine_initial_low"):
        assert k in m
    assert r["label"].startswith("SIMULATED")
    assert r["where_it_fails"] and isinstance(r["failures"], list)
    assert len(r["failures"]) == min(12, m["missed_genuine_in_low"])
    assert all(f["scenario"] in ("cardiac", "crash", "assault", "fire", "silent_medical") for f in r["failures"])


def test_threshold_sweep_shows_the_trade_off():
    sw = run_mix(42, 400)["sweep"]
    low = [s for s in sw if s["threshold"] == "low_max"]
    assert [s["missed_genuine_in_low"] for s in low] == sorted(s["missed_genuine_in_low"] for s in low)       # more LOW, more misses
    assert [s["false_alarms_flagged_pct"] for s in low] == sorted(s["false_alarms_flagged_pct"] for s in low)  # ...and more flagged
    high = [s for s in sw if s["threshold"] == "high_min"]
    assert high[0]["dispatcher_reviews_per_100"] <= high[-1]["dispatcher_reviews_per_100"]
    assert sum(s["current"] for s in sw) == 2


def test_the_mix_covers_genuine_and_accidental_cases():
    sc = {s["scenario"]: s for s in run_mix(42, 400)["scenarios"]}
    assert sc["cardiac"]["genuine"] and not sc["child_taps"]["genuine"]
    assert sc["crash"]["low"] == 0                                   # a crash signature is never LOW
    assert sc["test_press"]["low"] > 0


def test_api_endpoint_runs_the_mix_for_dispatchers_only(client):
    r = client.post("/api/simulation/false-alarm", headers=token_for(role="dispatcher"), json={"seed": 5, "n": 120})
    assert r.status_code == 200 and r.json()["metrics"]["calls"] == 120 and r.json()["model_version"] == "fa-v1-heuristic"
    assert client.post("/api/simulation/false-alarm", headers=token_for("caller-1"), json={}).status_code == 403
    assert client.post("/api/simulation/false-alarm", headers=token_for(role="dispatcher"), json={"n": 5}).status_code == 422
