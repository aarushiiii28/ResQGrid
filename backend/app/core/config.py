from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/resqgrid"
    test_database_url: str = "postgresql://postgres:postgres@localhost:5432/resqgrid_test"
    jwt_secret: str = "demo-only-secret-change-me-0123456789abcdef"
    jwt_ttl_s: int = 8 * 3600
    city_lat: float = 30.9010
    city_lng: float = 75.8573
    seed: int = 42
    routing_provider: str = "mock"
    osrm_url: str = "https://router.project-osrm.org"
    osrm_timeout_s: float = 3.0
    demo_autopilot: bool = True
    demo_autopilot_delay_s: float = 3.0
    llm_extraction: bool = False
    simulated_heartbeat: bool = True

    # freshness thresholds (seconds)
    vehicle_stale_s: float = 90.0
    hospital_stale_s: float = 1800.0
    # duplicate suggestion
    duplicate_window_s: float = 900.0
    duplicate_distance_m: float = 300.0
    duplicate_min_score: float = 0.5
    # temporary access reports expire unless reconfirmed
    access_report_ttl_s: float = 1800.0

    # dispatch score = wT*time + wF*freshness + wV*coverage_loss + wU*uncertainty + wS*switch
    # (all terms are in "minute-equivalents"; not clinically validated)
    w_time: float = 1.0
    w_fresh: float = 8.0
    w_coverage: float = 60.0
    w_uncertainty: float = 1.0
    w_switch: float = 8.0
    transport_weight: float = 0.5  # weight of scene->hospital leg relative to response leg
    min_reassign_benefit: float = 3.0  # score points a change must beat the current plan by
    candidate_vehicles: int = 6
    candidate_hospitals: int = 4
    coverage_radius_m: float = 2500.0
    batch_candidate_vehicles: int = 10

    # intake: after an SOS goes live, wait up to this long for the caller to say what happened (so the severity can be predicted and
    # the right ambulance type chosen) before dispatching with the safest assumption. 0 = dispatch at once.
    intake_wait_s: float = 25.0

    # acknowledgment watchdog
    ack_timeout_s: float = 60.0

    # risk monitor / recovery planner (hysteresis: raise above eta_raise, clear below eta_clear)
    risk_cooldown_s: float = 120.0
    eta_raise_factor: float = 1.25
    eta_clear_factor: float = 1.10
    eta_min_delta_s: float = 60.0
    risk_interval_s: float = 5.0
    risk_autopilot_delay_s: float = 3.0

    # demo simulation (clearly labelled simulated everywhere)
    sim_driver: bool = True
    sim_driver_delay_s: float = 2.0
    sim_on_scene_hold_s: float = 4.0
    sim_speed_factor: float = 15.0
    sim_tick_s: float = 0.5
    sim_volunteers: bool = True
    volunteer_radius_m: float = 1500.0
    db_skip_schema: bool = False                 # true when connecting as the restricted resqgrid_app role
    # M19 ledger. Defaults need no keys and no network (local anchor, demo master key).
    ledger_anchor_provider: str = "local"        # local | evm | fabric
    ledger_anchor_every_n: int = 25              # anchor after this many new entries...
    ledger_anchor_every_min: float = 10.0        # ...or when the oldest unanchored entry is this old
    ledger_runtime_dir: str = ""                 # server key + local anchor file; default <repo>/.runtime (git-ignored)
    ledger_server_key: str = ""                  # base64 Ed25519 private key (else generated into the runtime dir)
    ledger_master_key: str = ""                  # base64 32-byte key that wraps per-subject keys (demo default derived)
    require_signatures: bool = False             # true: reject unsigned important actions
    evm_rpc_url: str = "http://127.0.0.1:8545"
    evm_contract: str = ""
    evm_private_key: str = ""
    # M18: optional overrides of config/false_alarm.yaml timings (browser tests and demos only)
    fa_hold_ms: int | None = None
    fa_countdown_s: float | None = None
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
