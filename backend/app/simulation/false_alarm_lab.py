"""M18 in the Replay Lab: a seeded synthetic call mix run through the same scoring code as the live service.
Everything here is simulated; the traces are invented, so the numbers show behaviour of the model on this mix,
not accuracy on real emergencies. The goal is to show the trade-off and where it fails."""

import statistics
from datetime import UTC, datetime, timedelta

from app.services import false_alarm_model as fam

LABEL = "SIMULATED: synthetic call mix with invented sensor traces. Not real emergencies, not clinically validated."
LANE_ORDER = ("LOW", "UNCERTAIN", "HIGH")


def run_case(cfg: dict, case: dict) -> dict:
    tz = float(cfg.get("tz_offset_h", 0))
    now = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=case["hour"] - tz)
    base = dict(features=case["features"], mic_consent=case["mic_consent"], place_type=case["place_type"], now=now,
                device_accidents=case["device_accidents"], corroborating=case["corroborating"])
    a0 = fam.score(cfg, **base, checkin_state="none", explicit_help=False)
    ci = cfg["checkin"]
    if a0.lane == "HIGH" or a0.lane not in ci["lanes"]:
        final, verify_s, responded = a0, 0.0, "none"
    else:
        resp = case["checkin"] if case["checkin"] != "none" else "unanswered"
        responded = resp
        verify_s = case["response_s"] if resp != "unanswered" else float(ci["unanswered_at_s"])
        final = fam.score(cfg, **base, checkin_state=resp, explicit_help=case["explicit_help"])
    return {"scenario": case["scenario"], "genuine": case["genuine"], "initial_lane": a0.lane, "final_lane": final.lane,
            "p_initial": a0.probability_genuine, "p_final": final.probability_genuine, "verify_s": verify_s,
            "checkin": responded, "top": [{"feature": c.feature, "contribution": c.contribution} for c in fam.top_signals(final, 3)],
            "missing": final.missing}


def metrics(rows: list[dict], cfg: dict) -> dict:
    genuine = [r for r in rows if r["genuine"]]
    accidental = [r for r in rows if not r["genuine"]]
    hold = float(cfg["low_hold_s"])
    missed = [r for r in genuine if r["final_lane"] == "LOW"]                  # never auto-dispatched
    delayed = [r for r in genuine if r["initial_lane"] == "LOW"]               # held up to the verification window
    flagged = [r for r in accidental if r["final_lane"] == "LOW"]
    verified = [r["verify_s"] for r in rows if r["initial_lane"] != "HIGH"]
    n = max(1, len(rows))
    review = [r for r in rows if r["final_lane"] in ("LOW", "UNCERTAIN")]

    def pct(a: int, b: int) -> float:
        return round(100.0 * a / b, 1) if b else 0.0

    return {
        "calls": len(rows), "genuine": len(genuine), "accidental": len(accidental),
        "false_alarms_flagged": len(flagged), "false_alarms_flagged_pct": pct(len(flagged), len(accidental)),
        "missed_genuine_in_low": len(missed), "missed_genuine_in_low_pct": pct(len(missed), len(genuine)),
        "delayed_genuine_initial_low": len(delayed),
        "worst_delay_s": hold if delayed else 0.0,
        "accidental_dispatched": len(accidental) - len(flagged),
        "time_to_verify_median_s": round(statistics.median(verified), 1) if verified else 0.0,
        "time_to_verify_p90_s": round(sorted(verified)[int(0.9 * (len(verified) - 1))], 1) if verified else 0.0,
        "dispatcher_reviews": len(review), "dispatcher_reviews_per_100": round(100.0 * len(review) / n, 1),
    }


def scenario_table(rows: list[dict]) -> list[dict]:
    out = []
    for name, genuine in fam.SCENARIOS:
        rs = [r for r in rows if r["scenario"] == name]
        if not rs:
            continue
        out.append({"scenario": name, "genuine": genuine, "n": len(rs),
                    **{lane.lower(): sum(r["final_lane"] == lane for r in rs) for lane in LANE_ORDER},
                    "median_p": round(statistics.median(r["p_final"] for r in rs), 3)})
    return out


def sweep(cfg: dict, cases: list[dict]) -> list[dict]:
    """Threshold sweep: the LOW boundary trades flagged false alarms against missed genuine emergencies;
    the HIGH boundary trades dispatcher verification workload against dispatching without a second look."""
    out = []
    grid = [("low_max", v) for v in (0.05, 0.1, 0.15, 0.2, 0.25, 0.35, 0.45, 0.6)] + [("high_min", v) for v in (0.55, 0.65, 0.75, 0.85, 0.95)]
    for key, v in grid:
        c = fam.with_model(cfg, cfg["weights"], cfg["bias"], cfg["model_version"])
        c["lanes"] = {**cfg["lanes"], key: v}
        m = metrics([run_case(c, k) for k in cases], c)
        out.append({"threshold": key, "value": v, "false_alarms_flagged_pct": m["false_alarms_flagged_pct"],
                    "missed_genuine_in_low": m["missed_genuine_in_low"], "missed_genuine_in_low_pct": m["missed_genuine_in_low_pct"],
                    "dispatcher_reviews_per_100": m["dispatcher_reviews_per_100"],
                    "current": abs(v - cfg["lanes"][key]) < 1e-9})
    return out


def run_mix(seed: int = 42, n: int = 300, cfg: dict | None = None) -> dict:
    cfg = cfg or fam.load_config()
    cases = fam.SignalSimulator(seed).mix(n)
    rows = [run_case(cfg, c) for c in cases]
    failures = [{"scenario": r["scenario"], "p_final": r["p_final"], "initial_lane": r["initial_lane"], "checkin": r["checkin"],
                 "top": r["top"], "missing": r["missing"]} for r in rows if r["genuine"] and r["final_lane"] == "LOW"][:12]
    delayed = [r for r in rows if r["genuine"] and r["initial_lane"] == "LOW" and r["final_lane"] != "LOW"]
    return {
        "label": LABEL, "seed": seed, "n": n, "model_version": cfg["model_version"], "config_hash": fam.config_hash(cfg),
        "low_max": cfg["lanes"]["low_max"], "low_hold_s": cfg["low_hold_s"],
        "metrics": metrics(rows, cfg), "scenarios": scenario_table(rows), "sweep": sweep(cfg, cases),
        "failures": failures, "delayed": [{"scenario": r["scenario"], "verify_s": r["verify_s"], "final_lane": r["final_lane"]} for r in delayed][:12],
        "where_it_fails": [
            "Genuine emergencies that look like a deliberate test press (silent medical, phone on a table) are the hard case.",
            "Abandoned presses cannot be told apart from a person who cannot answer, so they are escalated, not dismissed.",
            "Sensors that are off or denied make the score less certain; unknown never lowers it, so some false alarms are still dispatched.",
        ],
    }
