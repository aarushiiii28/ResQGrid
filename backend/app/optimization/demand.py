"""M13 demand forecast and pre-positioning. PROTOTYPE APPROXIMATION on SYNTHETIC history.

Model (deliberately simple and explainable):
  * history: seeded synthetic incidents drawn from six hotspots, each with its own hours of the day.
  * time buckets: six 4-hour buckets x weekday/weekend.
  * per bucket, incidents are counted on a ~700 m grid, divided by the number of such days seen,
    smoothed with a small Gaussian kernel and a tiny uniform prior.
  * standby points: greedy maximum-coverage over demand-weighted cells within the response radius.
This is not an epidemiological or operational model; it only shows the mechanism."""

import math
import random
from dataclasses import dataclass

from app.core.geo import haversine_m
from app.routing.geometry import offset_point

LatLon = tuple[float, float]
BUCKETS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]
GRID_N = 24
HALF_KM = 8.0
SIGMA_CELLS = 1.0
PRIOR = 0.02

# name, offset east m, offset north m, spread m, weight, hour profile (peak hours), weekend factor
HOTSPOTS = [
    ("old market", -1500, 600, 700, 1.0, (10, 20), 1.3),
    ("bus station", 800, -900, 600, 0.8, (6, 10), 0.7),
    ("highway junction", 3200, 1500, 900, 0.9, (7, 10), 0.8),
    ("industrial estate", -3000, -2200, 1000, 0.7, (8, 17), 0.4),
    ("residential north", 400, 3300, 1100, 0.8, (18, 24), 1.2),
    ("stadium road", 2200, -2800, 800, 0.5, (16, 22), 1.8),
]


@dataclass(frozen=True)
class Hist:
    weekend: bool
    hour: int
    lat: float
    lon: float


def generate_history(center: LatLon, seed: int, days: int = 28, n: int = 700) -> list[Hist]:
    rng = random.Random(seed * 7919 + 13)
    out: list[Hist] = []
    day_types = [(d % 7) >= 5 for d in range(days)]
    while len(out) < n:
        weekend = rng.choice(day_types)
        name, east, north, spread, w, (h0, h1), wk = rng.choices(HOTSPOTS, weights=[h[4] for h in HOTSPOTS])[0]
        accept = (wk if weekend else 1.0) / 1.8
        if rng.random() > accept:
            continue
        hour = min(23, int(rng.triangular(h0, h1, (h0 + h1) / 2))) % 24
        lat, lon = offset_point(offset_point(center, 90, east + rng.gauss(0, spread)), 0, north + rng.gauss(0, spread))
        out.append(Hist(weekend, hour, round(lat, 6), round(lon, 6)))
    return out


def bucket_index(hour: int) -> int:
    return min(5, hour // 4)


def grid_cells(center: LatLon, n: int = GRID_N, half_km: float = HALF_KM) -> list[LatLon]:
    step = 2 * half_km * 1000 / n
    cells = []
    for i in range(n):
        for j in range(n):
            north = offset_point(center, 0, -half_km * 1000 + (i + 0.5) * step)
            cells.append(offset_point(north, 90, -half_km * 1000 + (j + 0.5) * step))
    return cells


def _cell_of(center: LatLon, lat: float, lon: float, n: int, half_km: float) -> int | None:
    step = 2 * half_km * 1000 / n
    dn = (lat - center[0]) * 111_320
    de = (lon - center[1]) * 111_320 * math.cos(math.radians(center[0]))
    i, j = int((dn + half_km * 1000) // step), int((de + half_km * 1000) // step)
    return i * n + j if 0 <= i < n and 0 <= j < n else None


def forecast_cells(history: list[Hist], center: LatLon, bucket: int, weekend: bool) -> tuple[list[float], dict]:
    """Expected incidents per hour per cell for one bucket and day type."""
    n = GRID_N
    days = 8 if weekend else 20  # 28 days = 20 weekdays + 8 weekend days
    raw = [0.0] * (n * n)
    used = 0
    for h in history:
        if h.weekend == weekend and bucket_index(h.hour) == bucket:
            c = _cell_of(center, h.lat, h.lon, n, HALF_KM)
            if c is not None:
                raw[c] += 1
                used += 1
    r = math.ceil(3 * SIGMA_CELLS)
    kernel = {(di, dj): math.exp(-(di * di + dj * dj) / (2 * SIGMA_CELLS**2)) for di in range(-r, r + 1) for dj in range(-r, r + 1)}
    ksum = sum(kernel.values())
    smooth = [0.0] * (n * n)
    for idx, v in enumerate(raw):
        if v == 0:
            continue
        i, j = divmod(idx, n)
        for (di, dj), k in kernel.items():
            a, b = i + di, j + dj
            if 0 <= a < n and 0 <= b < n:
                smooth[a * n + b] += v * k / ksum
    hours = BUCKETS[bucket][1] - BUCKETS[bucket][0]
    per_hour = [(s / days + PRIOR / (n * n)) / hours for s in smooth]
    return per_hour, {"history_used": used, "days_of_this_type": days, "bucket_hours": hours,
                      "bucket": f"{BUCKETS[bucket][0]:02d}-{BUCKETS[bucket][1]:02d}h", "day_type": "weekend" if weekend else "weekday"}


def coverage(points: list[LatLon], cells: list[LatLon], weights: list[float], radius_m: float) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    got = 0.0
    for c, w in zip(cells, weights, strict=True):
        if any(haversine_m(c[0], c[1], p[0], p[1]) <= radius_m for p in points):
            got += w
    return got / total


def suggest_standby(cells: list[LatLon], weights: list[float], k: int, radius_m: float,
                    step: int = 2) -> list[tuple[LatLon, float]]:
    """Greedy maximum coverage: each pick covers the most still-uncovered demand. Returns (point, gain share)."""
    n = GRID_N
    cands = [cells[i * n + j] for i in range(0, n, step) for j in range(0, n, step)]
    covered = [False] * len(cells)
    total = sum(weights) or 1.0
    picks: list[tuple[LatLon, float]] = []
    for _ in range(k):
        best, best_gain, best_cov = None, 0.0, None
        for c in cands:
            cov = [haversine_m(c[0], c[1], p[0], p[1]) <= radius_m for p in cells]
            gain = sum(w for w, cv, done in zip(weights, cov, covered, strict=True) if cv and not done)
            if gain > best_gain + 1e-12 or (abs(gain - best_gain) <= 1e-12 and best is not None and c < best and gain > 0):
                best, best_gain, best_cov = c, gain, cov
        if best is None or best_gain <= 0:
            break
        covered = [a or b for a, b in zip(covered, best_cov, strict=True)]
        picks.append((best, best_gain / total))
    return picks
