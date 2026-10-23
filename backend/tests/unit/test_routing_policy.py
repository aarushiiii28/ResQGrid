import copy

import pytest

from app.routing.geometry import point_at, polyline_length_m
from app.services import routing_policy as rp

P = rp.load_policy()


def obs(**kw):
    base = dict(t=1000.0, remaining_m=2000.0, cross_track_m=5.0, accuracy_m=8.0, remaining_eta_s=300.0, planned_remaining_s=300.0, blocked=False, off_confirmed=False)
    base.update(kw)
    return rp.Obs(**base)


def opt(eta, blocked=False, along=None, label=""):
    return rp.Option(eta_s=eta, blocked=blocked, rejoin_along_m=along, label=label)


def worse(pct, secs, planned=300.0):
    """An observation whose remaining ETA is worse than planned by exactly (pct %, secs s)."""
    return obs(planned_remaining_s=planned, remaining_eta_s=planned + secs)


# ---- hysteresis: ETA worse by more than 20 % AND 90 s, on two evaluations in a row ----------------------------------
def test_eta_threshold_boundaries_are_strict_on_both_percent_and_seconds():
    s = rp.TrackState()
    assert rp.assess(P, s, obs(planned_remaining_s=450, remaining_eta_s=450 + 90)).reasons == ["NO_MATERIAL_CHANGE"]       # exactly 90 s (20 %): not more than
    assert s.congestion_count == 0
    assert rp.assess(P, s, obs(planned_remaining_s=300, remaining_eta_s=300 + 60)).reasons == ["NO_MATERIAL_CHANGE"]        # 20 % but only 60 s
    assert rp.assess(P, s, obs(planned_remaining_s=1000, remaining_eta_s=1000 + 150)).reasons == ["NO_MATERIAL_CHANGE"]      # 150 s but only 15 %
    s = rp.TrackState()
    a = rp.assess(P, s, obs(planned_remaining_s=300, remaining_eta_s=300 + 91))                                           # 30 % and 91 s
    assert a.trigger == "none" and a.reasons == ["ETA_WORSE_NOT_SUSTAINED"] and s.congestion_count == 1


def test_two_consecutive_bad_evaluations_are_needed_and_one_good_one_resets_the_count():
    s = rp.TrackState()
    bad = obs(planned_remaining_s=300, remaining_eta_s=420)
    assert rp.assess(P, s, bad).trigger == "none" and s.congestion_count == 1
    assert rp.assess(P, s, obs()).reasons == ["NO_MATERIAL_CHANGE"] and s.congestion_count == 0                          # recovered: back to zero
    assert rp.assess(P, s, bad).trigger == "none" and s.congestion_count == 1                                              # starts again
    a = rp.assess(P, s, bad)
    assert a.trigger == "congestion" and a.reasons == ["ETA_WORSE_SUSTAINED"] and s.congestion_count == 2


# ---- worth it: saving, cooldown, final approach -----------------------------------------------------------------------------
def congested(s=None, **kw):
    s = s or rp.TrackState()
    o = obs(remaining_eta_s=400.0, planned_remaining_s=300.0, **kw)
    s.congestion_count = 2
    return s, o, rp.Assessment("congestion", ["ETA_WORSE_SUSTAINED"], {"remaining_eta_s": 400.0})


@pytest.mark.parametrize("alt_eta,expected", [(340.0, "reroute"), (341.0, "keep"), (380.0, "keep"), (250.0, "reroute")])
def test_alternative_must_save_at_least_60_s_and_15_percent(alt_eta, expected):
    s, o, a = congested()
    d = rp.choose(P, s, o, a, alternatives=[opt(alt_eta)])
    assert d.kind == expected
    if expected == "keep":
        assert "SAVING_TOO_SMALL" in d.reasons
    else:
        assert "WORTH_IT_SAVES_ENOUGH" in d.reasons and d.numbers["saving_s"] >= 60


def test_percent_saving_can_fail_while_seconds_pass():
    s = rp.TrackState()
    o = obs(remaining_eta_s=1000.0, planned_remaining_s=700.0)
    a = rp.Assessment("congestion", ["ETA_WORSE_SUSTAINED"], {})
    d = rp.choose(P, s, o, a, alternatives=[opt(900.0)])            # saves 100 s but only 10 %
    assert d.kind == "keep" and "SAVING_TOO_SMALL" in d.reasons and d.numbers["saving_pct"] == 10.0


def test_cooldown_blocks_a_second_reroute_for_60_seconds():
    s, o, a = congested()
    s.last_reroute_t = o.t - 59
    d = rp.choose(P, s, o, a, alternatives=[opt(200.0)])
    assert d.kind == "keep" and "COOLDOWN_ACTIVE" in d.reasons and d.numbers["since_last_reroute_s"] == 59.0
    s.last_reroute_t = o.t - 60
    assert rp.choose(P, s, o, a, alternatives=[opt(200.0)]).kind == "reroute"


def test_final_approach_is_locked_for_congestion_but_not_for_a_blocked_road():
    s, o, a = congested(remaining_m=300.0)
    d = rp.choose(P, s, o, a, alternatives=[opt(200.0)])
    assert d.kind == "keep" and "FINAL_APPROACH_LOCKED" in d.reasons
    assert rp.choose(P, s, congested(remaining_m=301.0)[1], a, alternatives=[opt(200.0)]).kind == "reroute"
    blocked = obs(remaining_m=120.0, blocked=True)
    ba = rp.assess(P, rp.TrackState(), blocked)
    assert ba.trigger == "blocked"
    assert rp.choose(P, rp.TrackState(), blocked, ba, rejoin=[opt(60.0, along=100.0)]).kind == "rejoin"        # still acts in the last 120 m


def test_no_clear_alternative_keeps_the_route_and_says_why():
    s, o, a = congested()
    d = rp.choose(P, s, o, a, alternatives=[opt(100.0, blocked=True)])
    assert d.kind == "keep" and "NO_CLEAR_ALTERNATIVE" in d.reasons
    assert rp.choose(P, s, o, a, alternatives=[]).kind == "keep"


# ---- wrong turn: consecutive fixes, good accuracy only ------------------------------------------------------------------------------
def test_off_route_needs_three_consecutive_good_fixes_beyond_45_m():
    s = rp.TrackState()
    f = lambda cross, acc=8.0: rp.observe_fix(P, s, cross_track=cross, accuracy_m=acc)  # noqa: E731
    assert f(44.0) == "on_route" and f(46.0) == "suspect" and f(60.0) == "suspect"
    assert f(10.0) == "on_route" and s.off_count == 0                       # one good fix resets
    assert [f(50.0), f(50.0), f(50.0)] == ["suspect", "suspect", "off_route"]


def test_poor_accuracy_is_uncertain_and_changes_nothing():
    s = rp.TrackState()
    rp.observe_fix(P, s, cross_track=80, accuracy_m=5)
    rp.observe_fix(P, s, cross_track=80, accuracy_m=5)
    assert rp.observe_fix(P, s, cross_track=500, accuracy_m=31) == "uncertain" and s.off_count == 2     # neither counted nor reset
    assert rp.observe_fix(P, s, cross_track=0, accuracy_m=250) == "uncertain" and s.off_count == 2
    assert rp.observe_fix(P, s, cross_track=80, accuracy_m=30) == "off_route"                              # 30 m is still good enough
    a = rp.assess(P, rp.TrackState(), obs(accuracy_m=45.0, cross_track_m=300.0))
    assert a.trigger == "uncertain" and a.reasons == ["GPS_ACCURACY_POOR_NO_ACTION"]
    assert rp.choose(P, rp.TrackState(), obs(accuracy_m=45.0), a).kind == "keep"


# ---- rejoin first, full replan only if clearly better -------------------------------------------------------------------------------------
def offroute():
    o = obs(off_confirmed=True, cross_track_m=180.0)
    return o, rp.assess(P, rp.TrackState(), o)


@pytest.mark.parametrize("rejoin,replan,expected", [
    (300.0, 271.0, "rejoin"),      # margin = max(30, 8 % of 300) = 30: needs <= 270
    (300.0, 270.0, "reroute"),
    (1000.0, 925.0, "rejoin"),     # margin = max(30, 80) = 80: needs <= 920
    (1000.0, 920.0, "reroute"),
    (300.0, 400.0, "rejoin"),
])
def test_full_replan_must_beat_the_best_rejoin_by_the_margin(rejoin, replan, expected):
    o, a = offroute()
    d = rp.choose(P, rp.TrackState(), o, a, rejoin=[opt(rejoin + 50, along=900), opt(rejoin, along=700)], replan=opt(replan))
    assert d.kind == expected
    assert d.numbers["rejoin_eta_s"] == rejoin and d.numbers["replan_eta_s"] == replan
    assert "WRONG_TURN_RECOVERY" in d.reasons
    assert ("FULL_REPLAN_BEATS_REJOIN" if expected == "reroute" else "REJOIN_PREFERRED") in d.reasons
    if expected == "rejoin":
        assert d.option.rejoin_along_m == 700                              # the rejoin point with the lowest remaining ETA


def test_rejoin_or_replan_when_only_one_exists_and_nothing_when_neither():
    o, a = offroute()
    assert rp.choose(P, rp.TrackState(), o, a, rejoin=[opt(200, along=500)]).kind == "rejoin"
    assert rp.choose(P, rp.TrackState(), o, a, rejoin=[opt(200, blocked=True)], replan=opt(300)).kind == "reroute"
    d = rp.choose(P, rp.TrackState(), o, a, rejoin=[opt(200, blocked=True)], replan=opt(300, blocked=True))
    assert d.kind == "keep" and "NO_USABLE_ROUTE" in d.reasons


def test_blocked_road_reroutes_immediately_and_prefers_rejoin():
    o = obs(blocked=True)
    s = rp.TrackState()
    a = rp.assess(P, s, o)
    assert a.trigger == "blocked" and a.reasons == ["ROAD_BLOCKED"]
    d = rp.choose(P, s, o, a, rejoin=[opt(260, along=800)], replan=opt(255))
    assert d.kind == "rejoin" and "BLOCKED_REROUTE_NOW" in d.reasons
    d = rp.choose(P, s, o, a, rejoin=[opt(400, blocked=True)], replan=opt(300))
    assert d.kind == "reroute"
    d = rp.choose(P, s, o, a, rejoin=[opt(400, blocked=True)], replan=opt(300, blocked=True))
    assert d.kind == "keep" and "BLOCKED_NO_CLEAR_ALTERNATIVE" in d.reasons


# ---- bookkeeping, determinism, geometry ----------------------------------------------------------------------------------------------------
def test_applying_a_decision_bumps_version_resets_counters_and_starts_the_cooldown():
    s = rp.TrackState()
    s.off_count, s.congestion_count = 3, 2
    d = rp.Decision("rejoin", ["X"], {"a": 1}, "stable-v1")
    rp.apply_decision(s, d, 500.0)
    assert (s.version, s.reroutes, s.last_reroute_t, s.off_count, s.congestion_count) == (2, 1, 500.0, 0, 0)
    rp.apply_decision(s, rp.Decision("keep", ["Y"], {}, "stable-v1"), 510.0)
    assert (s.version, s.reroutes) == (2, 1) and [h["decision"] for h in s.history] == ["rejoin", "keep"]


def test_decisions_are_deterministic_and_carry_the_policy_version():
    def run():
        s = rp.TrackState()
        out = []
        for eta in (400, 420, 430, 300):
            o = obs(remaining_eta_s=float(eta), planned_remaining_s=300.0)
            a = rp.assess(P, s, o)
            out.append(rp.choose(P, s, o, a, alternatives=[opt(250.0)]).as_log())
        return out

    assert run() == run()
    assert all(x["policy_version"] == "stable-v1" for x in run())


def test_config_changes_change_behaviour_without_code_changes():
    p2 = copy.deepcopy(P)
    p2["congestion"]["min_saving_s"] = 20
    p2["congestion"]["min_saving_pct"] = 5
    s, o, a = congested()
    assert rp.choose(P, s, o, a, alternatives=[opt(370.0)]).kind == "keep"
    assert rp.choose(p2, s, o, a, alternatives=[opt(370.0)]).kind == "reroute"


def test_route_geometry_helpers():
    pl = [(30.90, 75.85), (30.90, 75.86), (30.91, 75.86)]
    along, cross = rp.route_progress(pl, (30.9001, 75.855))
    assert 400 < along < 600 and 10 < cross < 15
    assert rp.cross_track_m(pl, (30.90, 75.855)) < 1
    assert rp.heading_difference_deg(pl, 300, 90) < 5 and rp.heading_difference_deg(pl, 300, 270) > 170
    assert rp.heading_difference_deg(pl, 300, None) is None
    cands = rp.rejoin_candidates(pl, 100.0, 1500.0, 150.0, 10)
    assert cands and all(100 < a <= 100 + 1500 for a, _ in cands) and len(cands) <= 10
    assert cands == sorted(cands, key=lambda c: c[0])
    end = rp.rejoin_candidates(pl, polyline_length_m(pl) - 200, 1500.0, 150.0, 10)
    assert end[-1][1] == point_at(pl, polyline_length_m(pl))                  # always offers the destination when it is within reach
