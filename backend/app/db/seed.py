"""Deterministic synthetic seed around the configured city centre. No real data."""

import math
import random
from datetime import timedelta
from typing import TYPE_CHECKING

from app.schemas.access import Aed, Building, Entrance, Volunteer
from app.schemas.common import GeoPoint, Source
from app.schemas.hospital import Hospital, Resource
from app.schemas.vehicle import Vehicle

if TYPE_CHECKING:
    from app.services.context import Ctx

# (tags per ambulance 1..20)
AMB_TAGS = (
    [["BLS"]] * 6
    + [["ALS"]] * 6
    + [["ALS", "cardiac"]] * 3
    + [["ALS", "neonatal"]] * 2
    + [["ALS", "trauma"]] * 3
)

# stable capabilities per hospital 1..12
HOSP_CAPS = [
    ["emergency", "cardiac", "stroke", "trauma", "neonatal", "burn"],
    ["emergency", "cardiac", "stroke"],
    ["emergency", "trauma"],
    ["emergency", "neonatal"],
    ["emergency", "cardiac"],
    ["emergency"],
    ["emergency", "stroke", "trauma"],
    ["emergency", "cardiac", "trauma"],
    ["emergency", "neonatal", "stroke"],
    ["emergency"],
    ["emergency", "burn", "trauma"],
    ["emergency", "cardiac", "stroke", "neonatal"],
]
CAP_TO_RES = {"cardiac": "cath_lab", "stroke": "ct_scanner", "trauma": "trauma_team",
              "neonatal": "nicu_cot", "emergency": "er_bay"}

# hospitals whose data is deliberately old / never confirmed so freshness handling is visible
STALE_DEMO = {"HOSP-05": 3 * 3600, "HOSP-09": 5 * 3600}
NEVER_CONFIRMED = {"HOSP-10"}


def offset(rng: random.Random, lat0: float, lon0: float, radius_m: float) -> tuple[float, float]:
    r = radius_m * math.sqrt(rng.random())
    th = rng.random() * 2 * math.pi
    dlat = (r * math.cos(th)) / 111_320
    dlon = (r * math.sin(th)) / (111_320 * math.cos(math.radians(lat0)))
    return round(lat0 + dlat, 6), round(lon0 + dlon, 6)


LOCALIZE_FAR_M = 15_000  # nearest available unit farther than this: the demo fleet is in another city
# ambulances always placed within NEAR_M of the centre: a mix of BLS, ALS, cardiac, neonatal and trauma
# so a nearby unit can serve any call type
NEAR_IDS = {1, 2, 7, 8, 13, 14, 16, 18, 19}
NEAR_M = 2_500


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(h))


async def localize_fleet(ctx: Ctx, lat: float, lon: float) -> bool:
    """Synthetic fleet follows the caller: when no available unit is near (e.g. the caller is in Amritsar
    and the seed is around Ludhiana), move the idle synthetic units around the caller. Units on a live job are
    never moved. Hospitals, buildings, AEDs and volunteers follow only when no hospital is within reach.
    Deterministic: same position, same fleet."""
    vehicles = await ctx.vehicle_repo.list()
    busy = {v.id for v in vehicles if v.status not in ("available", "unavailable")}
    free = [v for v in vehicles if v.status == "available" and v.location]
    if free and min(haversine_m((lat, lon), (v.location.lat, v.location.lon)) for v in free) <= LOCALIZE_FAR_M:
        return False
    hospitals = [h for h in await ctx.hospital_repo.list() if h.location]
    places = not hospitals or min(haversine_m((lat, lon), (h.location.lat, h.location.lon)) for h in hospitals) > 40_000
    await seed(ctx, seed_value=ctx.settings.seed + int(abs(lat) * 100) * 7 + int(abs(lon) * 100), centre=(lat, lon),
               skip_vehicles=busy, places=places)
    ctx.fleet_centre = (lat, lon)
    ctx.dispatch._grid = None  # coverage grid follows the new centre
    return True


async def seed(ctx: Ctx, seed_value: int | None = None, centre: tuple[float, float] | None = None,
               skip_vehicles: set[str] | None = None, places: bool = True) -> dict:
    s = ctx.settings
    rng = random.Random(s.seed if seed_value is None else seed_value)
    now = ctx.now()
    lat0, lon0 = centre or (s.city_lat, s.city_lng)

    for i, tags in enumerate(AMB_TAGS, start=1):
        la, lo = offset(rng, lat0, lon0, NEAR_M if i in NEAR_IDS else 6000)
        if f"AMB-{i:02d}" in (skip_vehicles or ()):
            continue
        pt = GeoPoint(lat=la, lon=lo, accuracy_m=15, at=now, source=Source.simulated)
        await ctx.vehicle_repo.put(
            Vehicle(
                id=f"AMB-{i:02d}", capability_tags=tags,
                status="unavailable" if i == 20 else "available",
                location=pt, location_at=now, state_at=now, last_update_at=now, base=pt,
            )
        )

    if not places:
        return {"vehicles": 20, "hospitals": 0, "buildings": 0, "aeds": 0, "volunteers": 0}

    for i, caps in enumerate(HOSP_CAPS, start=1):
        hid = f"HOSP-{i:02d}"
        la, lo = offset(rng, lat0, lon0, 7000)
        age = STALE_DEMO.get(hid, rng.randint(120, 900))
        confirmed = None if hid in NEVER_CONFIRMED else now - timedelta(seconds=age)
        res: dict[str, Resource] = {}
        for r in ("er_bay", "icu_bed", "cath_lab", "ct_scanner", "trauma_team", "nicu_cot"):
            has = r == "icu_bed" or any(CAP_TO_RES.get(c) == r for c in caps)
            if not has:
                res[r] = Resource(status="unavailable", count=0, last_confirmed_at=confirmed,
                                  source=Source.simulated)
            elif hid in NEVER_CONFIRMED:
                res[r] = Resource()
            else:
                busy = rng.random() < 0.15
                res[r] = Resource(
                    status="unavailable" if busy else "available",
                    count=0 if busy else rng.randint(1, 6),
                    last_confirmed_at=confirmed, source=Source.simulated,
                )
        await ctx.hospital_repo.put(
            Hospital(id=hid, name=f"Synthetic General {i:02d}",
                     location=GeoPoint(lat=la, lon=lo, source=Source.simulated),
                     capabilities=caps, resources=res, last_confirmed_at=confirmed,
                     source=Source.simulated)
        )

    kinds = ["main", "emergency", "rear", "gate", "side"]
    for i in range(1, 11):
        la, lo = offset(rng, lat0, lon0, 5000)
        ents = []
        for j in range(rng.choice([2, 3, 3])):
            ela, elo = offset(rng, la, lo, 70)
            ents.append(Entrance(
                id=f"B{i:02d}-E{j + 1}", name=f"Entrance {j + 1}", kind=kinds[(i + j) % 5],
                location=GeoPoint(lat=ela, lon=elo, accuracy_m=5, source=Source.simulated),
                accessible=rng.random() > 0.25, gate_restricted=(i + j) % 5 == 3,
                landmark=rng.choice(["near the pharmacy", "opposite the bus stop", "beside the ATM", None]),
                source=Source.simulated, confidence=round(rng.uniform(0.5, 0.95), 2),
                verified_at=now - timedelta(days=rng.randint(1, 60)),
            ))
        await ctx.building_repo.put(Building(
            id=f"BLD-{i:02d}", name=f"Synthetic Building {i:02d}",
            centroid=GeoPoint(lat=la, lon=lo, source=Source.simulated), entrances=ents))

    for i in range(1, 26):
        la, lo = offset(rng, lat0, lon0, 5000)
        await ctx.aed_repo.put(Aed(id=f"AED-{i:02d}", label=f"Synthetic AED {i:02d}",
                                   location=GeoPoint(lat=la, lon=lo, source=Source.simulated)))
    for i in range(1, 16):
        la, lo = offset(rng, lat0, lon0, 5000)
        await ctx.volunteer_repo.put(Volunteer(id=f"VOL-{i:02d}", name=f"Volunteer {i:02d} (synthetic)",
                                               location=GeoPoint(lat=la, lon=lo, source=Source.simulated),
                                               available=i % 5 != 0))
    return {"vehicles": 20, "hospitals": 12, "buildings": 10, "aeds": 25, "volunteers": 15}
