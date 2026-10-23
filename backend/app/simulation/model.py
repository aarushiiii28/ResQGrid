"""M11 deterministic world for the Replay Lab. SIMULATED: nothing here is real data and results must not
be read as predictions of real outcomes. The world is built once per seed and is immutable, so every
policy replays exactly the same incidents, faults and noise."""

import math
import random
from dataclasses import dataclass, field

from app.core.capabilities import CAP_RESOURCE, CATEGORY_HOSPITAL_CAPS, CATEGORY_VEHICLE_TAGS
from app.optimization import demand as dm
from app.routing.geometry import offset_point

LatLon = tuple[float, float]
CENTER: LatLon = (30.9010, 75.8573)

ALL_FAULTS = (
    "duplicate_calls",
    "stale_hospital_data",
    "blocked_entrance",
    "route_closure",
    "no_acknowledgment",
    "stale_gps",
    "offline_unit",
    "competing_incident",
)

HOSP_CAPS = [
    ["emergency", "cardiac", "stroke", "trauma", "neonatal"],
    ["emergency", "cardiac", "stroke"],
    ["emergency", "trauma"],
    ["emergency", "cardiac", "trauma"],
    ["emergency", "stroke", "neonatal"],
    ["emergency"],
    ["emergency", "cardiac", "stroke", "trauma"],
    ["emergency", "trauma", "neonatal"],
]
VEH_TAGS = [["BLS"]] * 3 + [["ALS"]] * 3 + [["ALS", "cardiac"]] * 3 + [["ALS", "trauma"]] * 2 + [["ALS", "neonatal"]]
CATS = [("cardiac", 0.30, 1), ("trauma", 0.25, 2), ("breathing", 0.15, 1), ("stroke", 0.10, 1), ("other", 0.20, 3)]


@dataclass
class SimConfig:
    seed: int = 42
    horizon_s: float = 7200.0
    n_vehicles: int = 11
    n_hospitals: int = 8
    n_incidents: int = 12
    faults: tuple[str, ...] = ALL_FAULTS
    speed_mps: float = 8.33
    eta_low: float = 0.85
    eta_high: float = 1.25
    processing_delay_s: float = 30.0  # call to decision
    ack_delay_range: tuple[float, float] = (8.0, 40.0)
    ack_timeout_s: float = 60.0
    manual_recheck_s: float = 420.0  # dispatcher radios the crew if nothing has happened
    callback_s: float = 900.0  # caller rings back if no ambulance has arrived
    gps_report_s: float = 30.0
    vehicle_stale_s: float = 90.0
    hospital_report_s: float = 600.0
    hospital_stale_s: float = 1500.0
    monitor_tick_s: float = 30.0
    approval_delay_s: float = 15.0  # human (or Demo Autopilot) approval of a recovery option
    confirm_reply_s: float = 45.0
    closure_known_after_s: float = 60.0
    blocked_entrance_delay_s: float = 180.0
    entrance_confirm_s: float = 90.0
    diversion_penalty_s: float = 120.0
    scene_time_s: float = 420.0
    dup_merge_s: float = 20.0
    coverage_radius_m: float = 2500.0
    jitter_gap: tuple[float, float] = (60.0, 110.0)


@dataclass
class SimHospital:
    id: str
    lat: float
    lon: float
    caps: frozenset[str]
    phase: float
    windows: dict[str, list[tuple[float, float]]]  # capability -> down windows
    stops_reporting_at: float | None = None

    def down(self, cap: str, t: float) -> bool:
        return any(a <= t < b for a, b in self.windows.get(cap, []))


@dataclass
class SimVehicle:
    id: str
    lat: float
    lon: float
    tags: frozenset[str]
    phase: float
    gps_gaps: list[tuple[float, float]] = field(default_factory=list)
    offline_at: float | None = None


@dataclass
class SimIncident:
    id: str  # report id; duplicates have dup_of set
    t_call: float
    category: str
    priority: int
    lat: float
    lon: float
    blocked_entrance: bool
    noack_first: bool
    noise_scene: float
    noise_hosp: float
    handover_base: float
    dup_of: str | None = None
    burst: bool = False
    text_sig: str = ""  # wording of the report; duplicate reports about one event share it

    def __post_init__(self) -> None:
        if not self.text_sig:
            self.text_sig = self.dup_of or self.id

    @property
    def real_id(self) -> str:
        return self.dup_of or self.id


@dataclass
class Closure:
    id: str
    lat: float
    lon: float
    radius: float
    t0: float
    t1: float


@dataclass
class World:
    cfg: SimConfig
    hospitals: list[SimHospital]
    vehicles: list[SimVehicle]
    reports: list[SimIncident]  # real incidents and duplicate reports, in call order
    closures: list[Closure]
    demand_cells: list[LatLon]
    demand_weights: dict[int, list[float]]  # bucket -> weights

    def req_tags(self, cat: str) -> set[str]:
        return set(CATEGORY_VEHICLE_TAGS[cat])

    def req_caps(self, cat: str) -> set[str]:
        return set(CATEGORY_HOSPITAL_CAPS[cat])


def manhattan_m(a: LatLon, b: LatLon) -> float:
    dn = abs(a[0] - b[0]) * 111_320
    de = abs(a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return dn + de


def build_world(cfg: SimConfig) -> World:
    """All randomness comes from one seeded generator drawn in a fixed order."""
    rng = random.Random(cfg.seed)
    hospitals: list[SimHospital] = []
    for i in range(cfg.n_hospitals):
        lat, lon = offset_point(offset_point(CENTER, 90, rng.uniform(-5000, 5000)), 0, rng.uniform(-5000, 5000))
        caps = frozenset(HOSP_CAPS[i % len(HOSP_CAPS)])
        windows: dict[str, list[tuple[float, float]]] = {}
        for c in sorted(caps):
            if rng.random() < 0.22:  # ordinary capacity churn: a resource goes down for a while
                a = rng.uniform(300, cfg.horizon_s - 1500)
                windows[c] = [(a, a + rng.uniform(1200, 2400))]
        hospitals.append(SimHospital(f"H{i + 1:02d}", round(lat, 6), round(lon, 6), caps, rng.uniform(0, cfg.hospital_report_s), windows))
    vehicles: list[SimVehicle] = []
    for i in range(cfg.n_vehicles):
        lat, lon = offset_point(offset_point(CENTER, 90, rng.uniform(-6000, 6000)), 0, rng.uniform(-6000, 6000))
        vehicles.append(SimVehicle(f"U{i + 1:02d}", round(lat, 6), round(lon, 6), frozenset(VEH_TAGS[i % len(VEH_TAGS)]), rng.uniform(0, cfg.gps_report_s)))

    faults = set(cfg.faults)
    reports: list[SimIncident] = []
    t = 0.0
    n_real = cfg.n_incidents
    gap = cfg.horizon_s * 0.8 / max(n_real, 1)
    for k in range(n_real):
        t += rng.expovariate(1 / gap) * 0.6 + 120
        cat, _, pr = rng.choices(CATS, weights=[c[1] for c in CATS])[0]
        lat, lon = offset_point(offset_point(CENTER, 90, rng.gauss(0, 2600)), 0, rng.gauss(0, 2600))
        reports.append(SimIncident(
            f"I{k + 1:02d}", round(t, 1), cat, pr, round(lat, 6), round(lon, 6),
            blocked_entrance="blocked_entrance" in faults and rng.random() < 0.30,
            noack_first="no_acknowledgment" in faults and rng.random() < 0.20,
            noise_scene=math.exp(rng.gauss(-0.016, 0.18)), noise_hosp=math.exp(rng.gauss(-0.016, 0.18)),
            handover_base=rng.uniform(480, 900)))
    # competing incidents: a burst in one area, most urgent last, while units are busy
    if "competing_incident" in faults:
        tb = rng.uniform(1500, 4200)
        blat, blon = offset_point(offset_point(CENTER, 90, rng.uniform(-2500, 2500)), 0, rng.uniform(-2500, 2500))
        for j, (cat, pr) in enumerate([("other", 3), ("other", 3), ("trauma", 2), ("cardiac", 1)]):
            lat, lon = offset_point(offset_point((blat, blon), 90, rng.uniform(-500, 500)), 0, rng.uniform(-500, 500))
            reports.append(SimIncident(f"B{j + 1:02d}", round(tb + j * 75, 1), cat, pr, round(lat, 6), round(lon, 6), False, False,
                                       math.exp(rng.gauss(-0.016, 0.18)), math.exp(rng.gauss(-0.016, 0.18)), rng.uniform(480, 900), burst=True))
    # duplicate calls about the same event: nearby, soon after, same wording
    if "duplicate_calls" in faults:
        for r in list(reports):
            if r.burst or rng.random() > 0.40:
                continue
            for d in range(rng.choice([1, 2])):
                lat, lon = offset_point(offset_point((r.lat, r.lon), 90, rng.uniform(-60, 60)), 0, rng.uniform(-60, 60))
                reports.append(SimIncident(f"{r.id}d{d + 1}", round(r.t_call + rng.uniform(20, 90), 1), r.category, r.priority,
                                           round(lat, 6), round(lon, 6), r.blocked_entrance, False, r.noise_scene, r.noise_hosp,
                                           r.handover_base, dup_of=r.id))
    reports.sort(key=lambda r: (r.t_call, r.id))

    closures: list[Closure] = []
    if "route_closure" in faults:
        real = [r for r in reports if r.dup_of is None]
        for c in range(rng.choice([1, 2])):
            tgt = rng.choice(real)
            lat, lon = offset_point(offset_point((tgt.lat, tgt.lon), rng.uniform(0, 360), rng.uniform(500, 1400)), 0, 0)
            t0 = max(60.0, tgt.t_call - rng.uniform(0, 400))
            closures.append(Closure(f"X{c + 1}", round(lat, 6), round(lon, 6), rng.uniform(700, 1300), round(t0, 1), round(t0 + rng.uniform(1500, 2700), 1)))
    if "stale_gps" in faults:
        for v in rng.sample(vehicles, 2):
            a = rng.uniform(600, cfg.horizon_s - 1800)
            v.gps_gaps.append((a, a + rng.uniform(600, 1500)))  # real outage
        for v in rng.sample(vehicles, 3):  # short blips: a good monitor must not overreact to these
            a = rng.uniform(300, cfg.horizon_s - 600)
            v.gps_gaps.append((a, a + rng.uniform(*cfg.jitter_gap)))
    if "offline_unit" in faults:
        v = rng.choice(vehicles)
        v.offline_at = rng.uniform(1200, 5000)
    if "stale_hospital_data" in faults:
        for h in rng.sample(hospitals, 2):
            t0 = rng.uniform(300, 3000)
            h.stops_reporting_at = t0
            for c in sorted(h.caps):
                h.windows[c] = [(t0 + 120, t0 + 120 + rng.uniform(2000, 3200))]  # it quietly goes down after the last report

    cells = dm.grid_cells(CENTER)
    hist = dm.generate_history(CENTER, cfg.seed)
    weights = {b: dm.forecast_cells(hist, CENTER, b, False)[0] for b in range(6)}
    return World(cfg, hospitals, vehicles, reports, closures, cells, weights)


def resource_of(cap: str) -> str:
    return CAP_RESOURCE.get(cap, "er_bay")
