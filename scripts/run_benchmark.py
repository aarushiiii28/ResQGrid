"""Compare P0 / P1 / P2 across seeds on identical simulated worlds.

    python scripts/run_benchmark.py [--seeds 20] [--faults all|none|a,b,c] [--json out.json]

SIMULATED results about a synthetic world. Not predictions of real outcomes; no lives-saved claims."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.simulation import metrics as mx  # noqa: E402
from app.simulation.model import ALL_FAULTS  # noqa: E402
from app.simulation.runner import LABEL, benchmark  # noqa: E402


def fmt(v, key):
    if v is None:
        return "   n/a"
    if key in ("coverage_mean", "eta_calibration", "detection_rate"):
        return f"{100 * v:5.1f}%"
    if key == "pct_meeting_requirements":
        return f"{v:5.1f}%"
    return f"{v:7.1f}" if abs(v) >= 100 or key.endswith("_s") else f"{v:7.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--faults", default="all")
    ap.add_argument("--json")
    a = ap.parse_args()
    faults = list(ALL_FAULTS) if a.faults == "all" else [] if a.faults == "none" else a.faults.split(",")
    seeds = list(range(1, a.seeds + 1))
    res = benchmark(seeds, faults)
    print(LABEL)
    print(f"seeds: {seeds[0]}..{seeds[-1]} ({len(seeds)})   faults: {', '.join(faults) or 'none'}\n")
    pols = res["policies"]
    print(f"{'metric (median across seeds, [p10 - p90])':58s}" + "".join(f"{p:>26s}" for p in pols))
    for k in mx.KEY_METRICS:
        row = f"{mx.LABELS[k]:58s}"
        for p in pols:
            d = res["distributions"][p][k]
            if d["n"] == 0:
                row += f"{'n/a':>26s}"
            else:
                row += f"{fmt(d['median'], k)} [{fmt(d['p10'], k).strip()} - {fmt(d['p90'], k).strip()}]".rjust(26)
        print(row)
    print("\nPaired on identical seeds (seeds where the first policy is better / worse than the second):")
    for pair, cmp in res["paired"].items():
        print(f"  {pair.replace('_vs_', ' vs ')}")
        for k, c in cmp.items():
            if c["better"] or c["worse"]:
                print(f"    {mx.LABELS[k]:52s} better {c['better']:2d}  worse {c['worse']:2d}  tied {c['tied']:2d}  mean delta {c['mean_delta']:+9.2f}")
    print("\nWhere P2 (ResQGrid) is worse than the P0 baseline:")
    if not res["where_p2_is_worse_than_p0"]:
        print("  nowhere on these seeds")
    for w in res["where_p2_is_worse_than_p0"]:
        print(f"  {w['label']}: worse on {w['seeds_worse']} of {w['of']} seeds (mean delta {w['mean_delta']:+.2f})")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
