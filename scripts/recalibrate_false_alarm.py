"""Fit new false-alarm weights from tagged SYNTHETIC calls (the seeded mix) and propose them as a new model.

    python scripts/recalibrate_false_alarm.py [--seed 7] [--n 1500] [--l2 1.0] [--propose]

The result is only a CANDIDATE with a new model_version. It changes nothing until an admin approves it
(POST /api/false-alarm/models/{version}/activate, audited). No silent model change, ever.
Synthetic data: the weights describe the mix, not real emergencies."""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import numpy as np  # noqa: E402

import app  # noqa: E402,F401  (sets the Windows event-loop policy)
from app.schemas.false_alarm import ModelProposal  # noqa: E402
from app.services import false_alarm_model as fam  # noqa: E402
from app.simulation.false_alarm_lab import metrics, run_case  # noqa: E402

FEATURES = list(fam.load_config()["weights"])


def evidence_matrix(cfg: dict, cases: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    tz = float(cfg.get("tz_offset_h", 0))
    for c in cases:
        now = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=c["hour"] - tz)
        resp = c["checkin"] if c["checkin"] != "none" else "unanswered"
        a = fam.score(cfg, features=c["features"], mic_consent=c["mic_consent"], place_type=c["place_type"], now=now,
                      device_accidents=c["device_accidents"], corroborating=c["corroborating"], checkin_state=resp,
                      explicit_help=c["explicit_help"])
        ev = {x.feature: x.evidence for x in a.contributions}
        xs.append([ev.get(f, 0.0) for f in FEATURES])
        ys.append(1.0 if c["genuine"] else 0.0)
    return np.array(xs), np.array(ys)


def _sig(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(z, -35, 35)))


def fit(x: np.ndarray, y: np.ndarray, l2: float, prior_w: np.ndarray, prior_b: float, iters: int = 4000) -> tuple[np.ndarray, float]:
    """Logistic regression by plain gradient descent with an L2 pull towards the current weights, so the
    fitted model cannot run away on separable synthetic data. Weights are kept within +-8."""
    n = len(y)
    w, b = prior_w.copy(), prior_b
    lr = 0.2
    for _ in range(iters):
        p = _sig(x @ w + b)
        g_w = x.T @ (p - y) / n + (l2 / n) * (w - prior_w)
        g_b = float(np.mean(p - y))
        w = np.clip(w - lr * g_w, -8, 8)
        b = float(np.clip(b - lr * g_b, -8, 8))
    return w, b


def log_loss(x: np.ndarray, y: np.ndarray, w: np.ndarray, b: float) -> float:
    p = np.clip(_sig(x @ w + b), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


async def propose(body: ModelProposal) -> dict:
    from app.core.config import get_settings
    from app.db.pg import Store
    from app.services.context import Ctx
    from app.websockets.manager import EventHub

    s = get_settings()
    store = Store(s.database_url)
    await store.open()
    try:
        ctx = Ctx(settings=s, store=store, hub=EventHub())
        return await ctx.false_alarm.propose_model(body, "recalibration-script")
    finally:
        await store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--l2", type=float, default=2.0)
    ap.add_argument("--propose", action="store_true", help="store the candidate in the database (still inactive)")
    a = ap.parse_args()
    cfg = fam.load_config()
    train = fam.SignalSimulator(a.seed).mix(a.n)
    test = fam.SignalSimulator(a.seed + 1000).mix(a.n)
    xt, yt = evidence_matrix(cfg, train)
    xe, ye = evidence_matrix(cfg, test)
    w0 = np.array([cfg["weights"][f] for f in FEATURES])
    w, b = fit(xt, yt, a.l2, w0, float(cfg["bias"]))
    weights = {f: round(float(v), 3) for f, v in zip(FEATURES, w, strict=True)}
    new_cfg = fam.with_model(cfg, weights, round(b, 3), "tmp")
    digest = hashlib.sha256(json.dumps([weights, round(b, 3)], sort_keys=True).encode()).hexdigest()[:8]
    version = f"fa-recal-s{a.seed}-{digest}"
    new_cfg["model_version"] = version
    before = metrics([run_case(cfg, c) for c in test], cfg)
    after = metrics([run_case(new_cfg, c) for c in test], new_cfg)
    result = {
        "version": version, "bias": round(b, 3), "weights": weights,
        "test_log_loss": {"current": round(log_loss(xe, ye, w0, float(cfg["bias"])), 4), "candidate": round(log_loss(xe, ye, w, b), 4)},
        "test_metrics": {"current": before, "candidate": after},
        "label": "SIMULATED: fitted on synthetic tagged calls; not validated on real emergencies",
    }
    print(json.dumps(result, indent=1))
    out = Path(__file__).resolve().parents[1] / ".runtime"
    out.mkdir(exist_ok=True)
    (out / f"{version}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    if a.propose:
        body = ModelProposal(version=version, weights=weights, bias=round(b, 3), note=result["label"],
                             metrics={"test_log_loss": result["test_log_loss"],
                                      "missed_genuine_in_low": [before["missed_genuine_in_low"], after["missed_genuine_in_low"]]})
        asyncio.run(propose(body))
        print(f"\nproposed {version} (inactive). An admin must approve it before it is used.")
    else:
        print("\ndry run: nothing stored. Add --propose to store the candidate (still inactive).")


if __name__ == "__main__":
    main()
