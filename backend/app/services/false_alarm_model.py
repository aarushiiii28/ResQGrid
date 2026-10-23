"""M18 Layer 5: explainable false-alarm score. Pure functions, no I/O, so the live service, the Replay Lab
and the recalibration script all use exactly the same arithmetic.

logit = bias + sum(weight_i * evidence_i), evidence in [-1, +1]; p(genuine) = sigmoid(logit).
Unavailable / denied / unknown signals have evidence 0: they are listed as missing and never lower the score.
Prototype heuristic. Not clinically validated."""

import hashlib
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from app.schemas.false_alarm import LANE_RANK, Assessment, Contribution, Feature

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "false_alarm.yaml"
MIC_FEATURES = ("scream", "impact_transient", "mic_silence", "mic_loudness")
PLACE_TYPES = ("highway", "junction", "residential", "school", "hospital", "market")


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))


def config_hash(cfg: dict) -> str:
    body = {k: cfg[k] for k in sorted(cfg) if k != "history"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def with_model(cfg: dict, weights: dict[str, float], bias: float, version: str) -> dict:
    out = json.loads(json.dumps(cfg))
    out["weights"], out["bias"], out["model_version"] = dict(weights), bias, version
    return out


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, x))))


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---- crash / fall detection ---------------------------------------------------------------------
def crash_like_confidence(peak_g: float | None, still_s: float | None, speed_drop_kmh: float | None) -> float | None:
    """Acceleration spike followed by stillness, plus a sudden GPS speed drop. Returns None if there is no
    accelerometer reading at all (unknown, not 'no crash')."""
    if peak_g is None:
        return None
    spike = clamp((peak_g - 2.0) / 4.0, 0, 1)           # 2 g = ordinary, 6 g+ = severe
    still = clamp((still_s or 0.0) / 8.0, 0, 1)          # stillness after the spike
    drop = clamp((speed_drop_kmh or 0.0) / 40.0, 0, 1)
    if spike <= 0:
        return 0.0
    base = spike * (0.55 + 0.45 * still)
    return round(clamp(base + 0.25 * drop * spike, 0, 1), 3)


# ---- synthetic place layer (seeded, no real map data) -----------------------------------------
def place_type_at(lat: float, lon: float, seed: int = 42) -> str:
    cell = (int(lat * 270), int(lon * 270))   # ~400 m cells
    h = hashlib.sha256(f"{seed}:{cell[0]}:{cell[1]}".encode()).digest()[0] / 255.0
    cuts = ((0.14, "highway"), (0.26, "junction"), (0.62, "residential"), (0.72, "school"), (0.80, "hospital"))
    for c, name in cuts:
        if h < c:
            return name
    return "market"


def hash_device(device_id: str | None, salt: str) -> str | None:
    if not device_id:
        return None
    return hashlib.sha256(f"{salt}:{device_id}".encode()).hexdigest()[:16]


# ---- scoring -----------------------------------------------------------------------------------
def _evidence(name: str, f: Feature | None, mic_consent: bool) -> tuple[float, str, str | None]:
    """(evidence in [-1,1], status, note) for one device feature."""
    if name in MIC_FEATURES and not mic_consent:
        return 0.0, "denied", "microphone analysis not consented"
    if f is None:
        return 0.0, "unavailable", "not reported"
    if f.status != "available" or f.value is None:
        return 0.0, f.status, "sensor " + f.status
    v = f.value
    if name == "speed_kmh":
        return clamp(v / 60.0, 0, 1), "available", f"{v:.0f} km/h"
    v = clamp(v, 0, 1)
    sign = {"touch_random": -1, "pocket_motion": -1, "device_idle": -1, "mic_silence": -1}.get(name, 1)
    return sign * v, "available", None


def _checkin_evidence(state: str) -> tuple[float, str, str | None]:
    if state == "safe":
        return -1.0, "derived", "caller tapped 'I am safe'"
    if state in ("help", "danger"):
        return 1.0, "derived", "caller asked for help"
    if state == "unanswered":
        return 1.0, "derived", "no answer to the check-in"
    return 0.0, "not_asked", None


def score(cfg: dict, *, features: dict[str, Feature], mic_consent: bool, place_type: str | None,
          now: datetime, device_accidents: int, corroborating: int, checkin_state: str,
          explicit_help: bool, trigger: str = "initial") -> Assessment:
    w, bias = cfg["weights"], float(cfg["bias"])
    contribs: list[Contribution] = []
    missing: list[str] = []

    def add(name: str, ev: float, status: str, note: str | None = None) -> None:
        weight = float(w.get(name, 0.0))
        contribs.append(Contribution(feature=name, evidence=round(ev, 4), weight=weight,
                                     contribution=round(weight * ev, 4), status=status, note=note))
        if status in ("unavailable", "denied"):
            missing.append(name)

    for name in ("crash_like", "scream", "impact_transient", "motion_erratic", "speed_context", "touch_deliberate",
                 "touch_random", "pocket_motion", "device_idle", "mic_silence"):
        key = "speed_kmh" if name == "speed_context" else name
        ev, st, note = _evidence(key, features.get(key), mic_consent)
        add(name, ev, st, note)

    if place_type:
        add("place_risk", float(cfg["place_weights"].get(place_type, 0.0)), "derived", place_type)
    else:
        add("place_risk", 0.0, "unavailable", "place unknown")
    local_hour = (now + timedelta(hours=float(cfg.get("tz_offset_h", 0)))).hour
    night = local_hour >= 22 or local_hour < 5
    add("night_time", 1.0 if night else 0.0, "derived", f"local hour {local_hour}")
    add("device_history", -min(1.0, device_accidents / 3.0), "derived", f"{device_accidents} earlier accidental press(es)")
    add("corroboration", min(1.0, corroborating / 2.0), "derived", f"{corroborating} other report(s) nearby")
    add("checkin", *_checkin_evidence(checkin_state))

    logit = bias + sum(c.contribution for c in contribs)
    p = sigmoid(logit)
    lanes = cfg["lanes"]
    base: str = "HIGH" if p >= lanes["high_min"] else "LOW" if p < lanes["low_max"] else "UNCERTAIN"
    lane, overrides = base, []
    ov = cfg["overrides"]

    def force(minimum: str, why: str) -> None:
        nonlocal lane
        if LANE_RANK[minimum] > LANE_RANK[lane]:
            lane = minimum
        if LANE_RANK[minimum] > LANE_RANK[base]:
            overrides.append(why)

    cl = features.get("crash_like")
    if cl and cl.status == "available" and cl.value is not None and cl.value >= ov["crash_like_min"]:
        force("HIGH", "crash_like")
    sc = features.get("scream")
    if mic_consent and sc and sc.status == "available" and sc.value is not None and sc.value >= ov["scream_min"]:
        force("UNCERTAIN", "scream")
    if explicit_help:
        force(ov["explicit_help"], "explicit_help")
    if checkin_state == "danger":
        force(ov["danger_confirmed"], "danger_confirmed")
    if checkin_state == "unanswered":
        force(ov["unanswered_checkin"], "unanswered_checkin")
    return Assessment(
        at=now, probability_genuine=round(p, 4), lane=lane, base_lane=base, logit=round(logit, 4), bias=bias,
        contributions=contribs, missing=missing, overrides=overrides, model_version=cfg["model_version"],
        config_hash=config_hash(cfg), trigger=trigger,
    )


def top_signals(a: Assessment, n: int = 4) -> list[Contribution]:
    return sorted((c for c in a.contributions if abs(c.contribution) > 1e-9), key=lambda c: -abs(c.contribution))[:n]


# ---- seeded signal simulator (tests, Replay Lab, recalibration) --------------------------------
SCENARIOS = (
    # (name, genuine?)
    ("cardiac", True), ("crash", True), ("assault", True), ("fire", True), ("silent_medical", True),
    ("child_taps", False), ("pocket_press", False), ("test_press", False), ("abandoned", False),
)


class SignalSimulator:
    """Seeded sensor traces per scenario. Some sensors are randomly unavailable or denied, as on real phones."""

    def __init__(self, seed: int):
        self.seed = seed

    def case(self, i: int, scenario: str) -> dict:
        r = random.Random(f"{self.seed}:{i}:{scenario}")
        f: dict[str, Feature] = {}

        def put(name: str, value: float | None, drop: float = 0.12) -> None:
            roll = r.random()
            if value is None or roll < drop:
                f[name] = Feature(name=name, value=None, status="denied" if roll < drop / 3 else "unavailable")
            else:
                f[name] = Feature(name=name, value=round(clamp(value, 0, 400), 3), status="available")

        def n(mu: float, sd: float) -> float:
            return clamp(r.gauss(mu, sd), 0, 1)

        mic = r.random() < 0.55            # consent to mic analysis
        place = r.choice(PLACE_TYPES)
        hour = r.randrange(24)
        accidents = 0
        corro = 0
        checkin = "none"
        help_text = False
        if scenario == "cardiac":
            put("motion_erratic", n(0.2, 0.15)); put("device_idle", n(0.5, 0.3)); put("touch_deliberate", n(0.8, 0.1))
            put("touch_random", n(0.1, 0.1)); put("pocket_motion", n(0.05, 0.05)); put("speed_kmh", 0)
            put("crash_like", None); put("scream", n(0.25, 0.2)); put("mic_silence", n(0.3, 0.2))
            checkin = r.choices(["help", "none", "safe"], [0.35, 0.5, 0.15])[0]
            help_text = r.random() < 0.4
            corro = 1 if r.random() < 0.1 else 0
        elif scenario == "crash":
            peak = r.uniform(4.5, 9.0)
            put("crash_like", crash_like_confidence(peak, r.uniform(2, 10), r.uniform(15, 60)), 0.1)
            put("speed_kmh", r.uniform(0, 12)); put("motion_erratic", n(0.5, 0.2)); put("impact_transient", n(0.8, 0.12))
            put("scream", n(0.4, 0.3)); put("touch_deliberate", n(0.6, 0.2)); put("touch_random", n(0.2, 0.15))
            put("pocket_motion", n(0.05, 0.05)); put("mic_silence", n(0.2, 0.15))
            place = r.choices(["highway", "junction", "residential"], [0.5, 0.3, 0.2])[0]
            checkin = r.choices(["help", "none", "safe"], [0.4, 0.45, 0.15])[0]
            corro = r.choices([0, 1, 2], [0.4, 0.4, 0.2])[0]
        elif scenario == "assault":
            put("scream", n(0.8, 0.15)); put("motion_erratic", n(0.8, 0.15)); put("touch_deliberate", n(0.4, 0.2))
            put("touch_random", n(0.4, 0.2)); put("impact_transient", n(0.5, 0.25)); put("crash_like", n(0.1, 0.1))
            put("pocket_motion", n(0.2, 0.15)); put("mic_silence", n(0.05, 0.05)); put("speed_kmh", r.uniform(0, 6))
            checkin = r.choices(["help", "none", "danger"], [0.3, 0.55, 0.15])[0]
            help_text = r.random() < 0.5
        elif scenario == "fire":
            put("scream", n(0.55, 0.25)); put("motion_erratic", n(0.55, 0.2)); put("touch_deliberate", n(0.7, 0.15))
            put("touch_random", n(0.15, 0.1)); put("pocket_motion", n(0.1, 0.1)); put("mic_silence", n(0.1, 0.1))
            put("impact_transient", n(0.3, 0.2)); put("crash_like", n(0.1, 0.1)); put("speed_kmh", r.uniform(0, 8))
            checkin = r.choices(["help", "none", "safe"], [0.4, 0.45, 0.15])[0]
            corro = r.choices([0, 1, 2], [0.3, 0.4, 0.3])[0]
            help_text = r.random() < 0.5
        elif scenario == "silent_medical":
            # elderly, phone on the table, cannot speak: looks almost like a test press. Hard case by design.
            put("motion_erratic", n(0.05, 0.05)); put("device_idle", n(0.8, 0.12)); put("touch_deliberate", n(0.85, 0.1))
            put("touch_random", n(0.05, 0.05)); put("pocket_motion", n(0.02, 0.02)); put("speed_kmh", 0)
            put("crash_like", None); put("scream", n(0.05, 0.05)); put("mic_silence", n(0.7, 0.15)); put("impact_transient", n(0.05, 0.05))
            place = r.choices(["residential", "market", "hospital"], [0.8, 0.1, 0.1])[0]
            hour = r.randrange(24)
            checkin = r.choices(["none", "safe", "help"], [0.7, 0.2, 0.1])[0]
        elif scenario == "child_taps":
            put("touch_random", n(0.85, 0.1)); put("touch_deliberate", n(0.15, 0.1)); put("motion_erratic", n(0.45, 0.2))
            put("pocket_motion", n(0.1, 0.1)); put("device_idle", n(0.2, 0.2)); put("scream", n(0.15, 0.15))
            put("mic_silence", n(0.3, 0.2)); put("crash_like", n(0.1, 0.1)); put("impact_transient", n(0.1, 0.1)); put("speed_kmh", 0)
            place = r.choices(["residential", "school", "market"], [0.5, 0.35, 0.15])[0]
            accidents = r.choices([0, 1, 2, 3], [0.3, 0.3, 0.2, 0.2])[0]
            checkin = r.choices(["safe", "none"], [0.65, 0.35])[0]
        elif scenario == "pocket_press":
            put("pocket_motion", n(0.85, 0.1)); put("touch_random", n(0.5, 0.2)); put("touch_deliberate", n(0.2, 0.15))
            put("motion_erratic", n(0.4, 0.2)); put("mic_silence", n(0.7, 0.15)); put("scream", n(0.05, 0.05))
            put("crash_like", n(0.08, 0.08)); put("impact_transient", n(0.15, 0.1)); put("device_idle", n(0.05, 0.05))
            put("speed_kmh", r.uniform(0, 7))
            place = r.choices(["residential", "market", "junction", "highway"], [0.3, 0.3, 0.2, 0.2])[0]
            accidents = r.choices([0, 1, 2], [0.5, 0.3, 0.2])[0]
            checkin = r.choices(["safe", "none"], [0.7, 0.3])[0]
        elif scenario == "test_press":
            put("touch_deliberate", n(0.9, 0.08)); put("touch_random", n(0.05, 0.05)); put("device_idle", n(0.6, 0.2))
            put("motion_erratic", n(0.05, 0.05)); put("pocket_motion", n(0.02, 0.02)); put("mic_silence", n(0.45, 0.2))
            put("scream", n(0.03, 0.03)); put("crash_like", 0.0); put("impact_transient", n(0.05, 0.05)); put("speed_kmh", 0)
            place = r.choices(["hospital", "residential", "market"], [0.25, 0.55, 0.2])[0]
            hour = r.randrange(9, 19)
            accidents = r.choices([0, 1, 2, 3], [0.3, 0.2, 0.2, 0.3])[0]
            checkin = r.choices(["safe", "none"], [0.85, 0.15])[0]
        else:  # abandoned: pressed, hold completed, then nothing
            put("touch_deliberate", n(0.5, 0.2)); put("touch_random", n(0.2, 0.15)); put("device_idle", n(0.5, 0.25))
            put("motion_erratic", n(0.1, 0.1)); put("pocket_motion", n(0.2, 0.2)); put("mic_silence", n(0.6, 0.2))
            put("scream", n(0.05, 0.05)); put("crash_like", n(0.05, 0.05)); put("impact_transient", n(0.05, 0.05)); put("speed_kmh", 0)
            accidents = r.choices([0, 1, 2], [0.5, 0.3, 0.2])[0]
            checkin = r.choices(["none", "safe"], [0.9, 0.1])[0]
        if not mic:
            for k in MIC_FEATURES:
                f.pop(k, None)
        genuine = dict(SCENARIOS)[scenario]
        resp_s = round(r.uniform(4, 38), 1)
        return {"scenario": scenario, "genuine": genuine, "features": f, "mic_consent": mic, "place_type": place,
                "hour": hour, "device_accidents": accidents, "corroborating": corro, "checkin": checkin,
                "explicit_help": help_text, "response_s": resp_s}

    def mix(self, n: int) -> list[dict]:
        """Seeded call mix: roughly 28% genuine, the rest accidental, abandoned or test presses."""
        r = random.Random(f"mix:{self.seed}")
        names = [s for s, _ in SCENARIOS]
        weights = [0.07, 0.07, 0.04, 0.04, 0.06, 0.2, 0.2, 0.14, 0.18]
        return [self.case(i, r.choices(names, weights)[0]) for i in range(n)]
