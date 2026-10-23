import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings
from app.db.pg import Store
from app.db.repos import Repo
from app.schemas.access import Aed, Building, Volunteer
from app.schemas.common import utcnow
from app.schemas.hospital import Hospital
from app.schemas.incident import Incident, Report
from app.schemas.vehicle import Vehicle
from app.services.access_service import AccessService
from app.services.ack_service import AckService
from app.services.alerts_service import AlertsService
from app.services.audit_service import AuditService
from app.services.autopilot import AutopilotService
from app.services.call_service import CallService
from app.services.demand_service import DemandService
from app.services.demo_driver import DemoDriverService
from app.services.dispatch_service import DispatchService
from app.services.driver_events import DriverEventService
from app.services.extraction import ExtractionService
from app.services.false_alarm_service import FalseAlarmService
from app.services.hospital_case_service import HospitalCaseService
from app.services.hospital_service import HospitalService
from app.services.incident_service import IncidentService
from app.services.ledger_service import LedgerService
from app.services.mci_service import MciService
from app.services.nav_service import NavService
from app.services.pipeline import PipelineService
from app.services.risk_service import RiskService
from app.services.route_stability_service import RouteStabilityService
from app.services.routing_policy import load_policy
from app.services.routing_service import RoutingService, build_provider
from app.services.simulation_service import SimulationService
from app.services.tracker_service import TrackerService
from app.services.vehicle_service import VehicleService
from app.services.volunteer_service import VolunteerService
from app.websockets.manager import EventHub


@dataclass
class Ctx:
    settings: Settings
    store: Store
    hub: EventHub
    clock: Callable[[], datetime] = utcnow
    extras: dict = field(default_factory=dict)

    def now(self) -> datetime:
        return self.clock()

    def __post_init__(self):
        s = self.store
        self.incident_repo = Repo(s, "incident", Incident)
        self.report_repo = Repo(s, "report", Report)
        self.vehicle_repo = Repo(s, "vehicle", Vehicle)
        self.hospital_repo = Repo(s, "hospital", Hospital)
        self.building_repo = Repo(s, "building", Building)
        self.aed_repo = Repo(s, "aed", Aed)
        self.volunteer_repo = Repo(s, "volunteer", Volunteer)
        self.audit = AuditService(s, self.hub, self.now)
        self.ledger = LedgerService(self)
        self.audit.ledger = self.ledger
        self.incidents = IncidentService(
            self.incident_repo, self.report_repo, s, self.audit, self.settings, self.now
        )
        self.vehicles = VehicleService(
            self.vehicle_repo, self.incident_repo, self.audit, self.hub, self.settings, self.now
        )
        self.hospitals = HospitalService(self.hospital_repo, self.audit, self.settings, self.now)
        self.routing = RoutingService(
            self.store, build_provider(self.settings), self.audit, self.hub, self.settings, self.now
        )
        self.access = AccessService(self.building_repo, self.audit, self.store, self.settings, self.now)
        self.dispatch = DispatchService(self)
        self.acks = AckService(self)
        self.autopilot = AutopilotService(self)
        self.extraction = ExtractionService(self.settings)
        self.calls = CallService(self)
        self.false_alarm = FalseAlarmService(self)
        self.pipeline = PipelineService(self)
        self.cases = HospitalCaseService(self)
        self.stability = RouteStabilityService(self)
        self.nav = NavService(self)
        self.driver_events = DriverEventService(self)
        self.demo_driver = DemoDriverService(self)
        self.volunteers = VolunteerService(self)
        self.tracker = TrackerService(self)
        self.alerts = AlertsService(self)
        self.risks = RiskService(self)
        self.mci = MciService(self)
        self.demand = DemandService(self)
        self.simulation = SimulationService(self)
        self.tasks: set[asyncio.Task] = set()
        self.fleet_centre: tuple[float, float] | None = None  # set when the demo fleet follows a caller

    def spawn(self, coro) -> asyncio.Task:
        """Run a background coroutine (pipeline, simulated crew). Exceptions are swallowed so one
        failed demo step cannot take the server down; they surface as missing steps/alerts."""

        async def guard():
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                try:
                    await self.audit.record(entity_type="system", entity_id="background", event_type="background.error",
                                            actor="system", payload={"error": f"{type(e).__name__}: {e}"})
                except Exception:  # noqa: BLE001
                    pass

        t = asyncio.create_task(guard())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for all background work (tests, benchmark)."""
        async def _wait():
            while self.tasks or self.autopilot.tasks:
                await asyncio.gather(*list(self.tasks), *list(self.autopilot.tasks), return_exceptions=True)

        await asyncio.wait_for(_wait(), timeout)

    def reset_runtime(self) -> None:
        """Forget in-memory simulation state (used by demo reset: ids restart, so old routes must not leak)."""
        for t in list(self.tasks) + list(self.autopilot.tasks):
            t.cancel()
        self.nav.state.clear()
        self.nav.tasks.clear()
        self.demo_driver.tasks.clear()
        self.pipeline.sig.clear()
        self.extras.clear()
        self.ledger.reset_runtime()
        self.stability.policy = load_policy()  # back to the configured stable-route policy
        self.fleet_centre = None
        self.dispatch._grid = None
