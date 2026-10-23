"""P0 baseline: nearest eligible available vehicle by straight-line distance, and the nearest
hospital that has the required capability tag. Assumptions: ignores road network, resource
freshness, coverage and reassignment cost."""

from app.core.geo import haversine_m
from app.schemas.hospital import Hospital
from app.schemas.vehicle import Vehicle

LatLon = tuple[float, float]


def nearest_vehicle(vehicles: list[Vehicle], scene: LatLon) -> Vehicle | None:
    live = [v for v in vehicles if v.location is not None]
    if not live:
        return None
    return min(live, key=lambda v: (haversine_m(scene[0], scene[1], v.location.lat, v.location.lon), v.id))


def nearest_capable_hospital(hospitals: list[Hospital], scene: LatLon, required_caps: set[str]) -> Hospital | None:
    ok = [h for h in hospitals if required_caps <= set(h.capabilities)]
    if not ok:
        return None
    return min(ok, key=lambda h: (haversine_m(scene[0], scene[1], h.location.lat, h.location.lon), h.id))
