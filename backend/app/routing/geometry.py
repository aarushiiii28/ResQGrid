"""Small planar geometry helpers (local equirectangular projection; fine at city scale)."""

import math

from app.core.geo import haversine_m

LatLon = tuple[float, float]
M_PER_DEG = 111_320.0


def to_xy(p: LatLon, ref: LatLon) -> tuple[float, float]:
    return (p[1] - ref[1]) * M_PER_DEG * math.cos(math.radians(ref[0])), (p[0] - ref[0]) * M_PER_DEG


def from_xy(x: float, y: float, ref: LatLon) -> LatLon:
    return ref[0] + y / M_PER_DEG, ref[1] + x / (M_PER_DEG * math.cos(math.radians(ref[0])))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(bearing: float) -> str:
    return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][
        int((bearing + 22.5) % 360 // 45)
    ]


def seg_point_dist_m(a: LatLon, b: LatLon, p: LatLon) -> float:
    ax, ay = to_xy(a, p)
    bx, by = to_xy(b, p)
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
    return math.hypot(ax + t * dx, ay + t * dy)


def polyline_length_m(pl: list[LatLon]) -> float:
    return sum(haversine_m(*pl[i], *pl[i + 1]) for i in range(len(pl) - 1))


def path_hits_circle(pl: list[LatLon], center: LatLon, radius_m: float) -> bool:
    return any(seg_point_dist_m(pl[i], pl[i + 1], center) < radius_m for i in range(len(pl) - 1))


def offset_point(p: LatLon, bearing: float, dist_m: float) -> LatLon:
    x = dist_m * math.sin(math.radians(bearing))
    y = dist_m * math.cos(math.radians(bearing))
    return from_xy(x, y, p)


def densify(pl: list[LatLon], spacing_m: float = 100.0, max_points: int = 250) -> list[LatLon]:
    """Add interpolated points so a simulated vehicle can move smoothly along the route."""
    total = polyline_length_m(pl)
    spacing = max(spacing_m, total / max_points) if total else spacing_m
    out: list[LatLon] = [pl[0]]
    for a, b in zip(pl, pl[1:], strict=False):
        d = haversine_m(*a, *b)
        n = max(1, int(d // spacing))
        for k in range(1, n + 1):
            f = k / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def project_along(pl: list[LatLon], p: LatLon) -> float:
    """Distance (m) along the polyline of the point nearest to p."""
    best, best_d, cum = 0.0, float("inf"), 0.0
    for i in range(len(pl) - 1):
        a, b = pl[i], pl[i + 1]
        seg = haversine_m(*a, *b)
        ax, ay = to_xy(a, p)
        bx, by = to_xy(b, p)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best_d:
            best_d, best = d, cum + t * seg
        cum += seg
    return best


def point_at(pl: list[LatLon], along_m: float) -> LatLon:
    if along_m <= 0:
        return pl[0]
    cum = 0.0
    for i in range(len(pl) - 1):
        a, b = pl[i], pl[i + 1]
        seg = haversine_m(*a, *b)
        if cum + seg >= along_m and seg > 0:
            f = (along_m - cum) / seg
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        cum += seg
    return pl[-1]


def tail_from(pl: list[LatLon], along_m: float) -> list[LatLon]:
    """The part of the polyline still ahead of `along_m`."""
    cum = 0.0
    for i in range(len(pl) - 1):
        seg = haversine_m(*pl[i], *pl[i + 1])
        if cum + seg > along_m:
            return [point_at(pl, along_m), *pl[i + 1 :]]
        cum += seg
    return [pl[-1], pl[-1]]
