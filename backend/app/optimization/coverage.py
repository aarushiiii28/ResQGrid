"""Prototype coverage approximation: the share of a uniform grid of demand points that has at
least one available unit within a straight-line response radius. Not a validated coverage model;
Phase 5 swaps the uniform weights for the seeded demand forecast."""

from app.core.geo import haversine_m
from app.routing.geometry import offset_point

LatLon = tuple[float, float]


def make_grid(center: LatLon, half_km: float = 6.0, n: int = 13) -> list[LatLon]:
    pts: list[LatLon] = []
    step = 2 * half_km * 1000 / (n - 1)
    for i in range(n):
        for j in range(n):
            north = offset_point(center, 0, -half_km * 1000 + i * step)
            pts.append(offset_point(north, 90, -half_km * 1000 + j * step))
    return pts


def coverage_fraction(units: list[LatLon], grid: list[LatLon], radius_m: float) -> float:
    if not grid:
        return 0.0
    covered = sum(
        1 for g in grid if any(haversine_m(g[0], g[1], u[0], u[1]) <= radius_m for u in units)
    )
    return covered / len(grid)


def coverage_loss(units: dict[str, LatLon], remove_id: str, grid: list[LatLon], radius_m: float) -> float:
    """Coverage fraction lost if `remove_id` is dispatched away. 0 if it is not an available unit."""
    if remove_id not in units:
        return 0.0
    before = coverage_fraction(list(units.values()), grid, radius_m)
    after = coverage_fraction([p for k, p in units.items() if k != remove_id], grid, radius_m)
    return max(0.0, before - after)
