from datetime import UTC, datetime

from app.schemas.false_alarm import Feature
from app.services import false_alarm_model as fam

NOW = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)  # 12:30 local, daytime
CFG = fam.load_config()


def feat(name, value, status="available"):
    return Feature(name=name, value=value, status=status)


def run(features=None, **kw):
    base = dict(features=features or {}, mic_consent=True, place_type="residential", now=NOW, device_accidents=0,
                corroborating=0, checkin_state="none", explicit_help=False)
    base.update(kw)
    return fam.score(CFG, **base)


def test_no_signals_is_uncertain_not_low():
    a = run()
    assert a.lane == "UNCERTAIN" and 0.5 < a.probability_genuine < 0.75


def test_unavailable_and_denied_sensors_never_lower_the_score():
    base = run()
    every = ["crash_like", "scream", "impact_transient", "motion_erratic", "touch_deliberate", "touch_random",
             "pocket_motion", "device_idle", "mic_silence", "speed_kmh"]
    unavailable = run({n: feat(n, None, "unavailable") for n in every})
    denied = run({n: feat(n, None, "denied") for n in every})
    assert unavailable.probability_genuine == base.probability_genuine == denied.probability_genuine
    assert "crash_like" in unavailable.missing and "scream" in denied.missing


def test_microphone_features_ignored_without_consent():
    with_mic = run({"scream": feat("scream", 0.9)}, mic_consent=True)
    without = run({"scream": feat("scream", 0.9)}, mic_consent=False)
    assert with_mic.probability_genuine > without.probability_genuine
    assert without.probability_genuine == run().probability_genuine
    assert "scream" in without.missing


def test_explanation_sums_to_the_logit_and_probability():
    a = run({"crash_like": feat("crash_like", 0.7), "touch_random": feat("touch_random", 0.8), "pocket_motion": feat("pocket_motion", 0.5)},
            place_type="highway", device_accidents=2, corroborating=1)
    assert abs(a.bias + sum(c.contribution for c in a.contributions) - a.logit) < 0.01
    assert abs(fam.sigmoid(a.logit) - a.probability_genuine) < 1e-3
    assert all(abs(c.weight * c.evidence - c.contribution) < 1e-3 for c in a.contributions)


def test_hard_overrides_set_minimum_lanes():
    # a strongly accidental picture, but a crash signature: HIGH regardless
    accidental = {"touch_random": feat("touch_random", 1.0), "pocket_motion": feat("pocket_motion", 1.0)}
    a = run({**accidental, "crash_like": feat("crash_like", 0.9)}, device_accidents=3, place_type="hospital", checkin_state="safe")
    assert a.lane == "HIGH" and "crash_like" in a.overrides
    low = run(accidental, device_accidents=3, place_type="hospital", checkin_state="safe")
    assert low.lane == "LOW"
    assert run(accidental, device_accidents=3, place_type="hospital", checkin_state="safe", explicit_help=True).lane == "HIGH"
    assert run({**accidental, "scream": feat("scream", 0.9)}, device_accidents=3, place_type="hospital", checkin_state="safe").lane != "LOW"
    assert run(accidental, device_accidents=3, place_type="hospital", checkin_state="unanswered").lane != "LOW"
    assert run(accidental, device_accidents=3, place_type="hospital", checkin_state="danger").lane == "HIGH"


def test_safe_tap_lowers_the_score_but_never_by_itself_closes_anything():
    before = run({"touch_deliberate": feat("touch_deliberate", 0.9)})
    after = run({"touch_deliberate": feat("touch_deliberate", 0.9)}, checkin_state="safe")
    assert after.probability_genuine < before.probability_genuine


def test_lane_thresholds():
    assert run({"crash_like": feat("crash_like", 0.5), "scream": feat("scream", 0.5)}, place_type="highway", corroborating=2).lane == "HIGH"
    lows = run({"touch_random": feat("touch_random", 0.9), "pocket_motion": feat("pocket_motion", 0.8)}, device_accidents=2)
    assert lows.lane == "LOW" and lows.probability_genuine < CFG["lanes"]["low_max"]


def test_crash_like_needs_spike_stillness_and_speed_drop():
    assert fam.crash_like_confidence(None, 5, 30) is None            # no accelerometer: unknown, not "no crash"
    assert fam.crash_like_confidence(1.2, 9, 50) == 0.0              # no spike
    hard = fam.crash_like_confidence(8.0, 9, 45)
    soft = fam.crash_like_confidence(3.0, 1, 0)
    assert hard > 0.9 and soft < 0.4


def test_config_hash_changes_with_weights_and_model_version_is_recorded():
    other = fam.with_model(CFG, {**CFG["weights"], "crash_like": 1.0}, CFG["bias"], "fa-v2")
    assert fam.config_hash(other) != fam.config_hash(CFG)
    a = fam.score(other, features={}, mic_consent=False, place_type=None, now=NOW, device_accidents=0, corroborating=0,
                  checkin_state="none", explicit_help=False)
    assert a.model_version == "fa-v2" and a.config_hash == fam.config_hash(other)


def test_signal_simulator_is_deterministic_and_covers_scenarios():
    a, b = fam.SignalSimulator(7).mix(60), fam.SignalSimulator(7).mix(60)
    assert [x["scenario"] for x in a] == [x["scenario"] for x in b]
    assert [{k: v.model_dump() for k, v in x["features"].items()} for x in a] == [{k: v.model_dump() for k, v in x["features"].items()} for x in b]
    assert {x["scenario"] for x in fam.SignalSimulator(7).mix(400)} == {s for s, _ in fam.SCENARIOS}
    assert [x["scenario"] for x in fam.SignalSimulator(8).mix(60)] != [x["scenario"] for x in a]


def test_place_layer_is_seeded_and_stable():
    assert fam.place_type_at(30.9, 75.85, 42) == fam.place_type_at(30.9, 75.85, 42)
    kinds = {fam.place_type_at(30.9 + i * 0.004, 75.85 + j * 0.004, 42) for i in range(30) for j in range(30)}
    assert kinds == set(fam.PLACE_TYPES)
