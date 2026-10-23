import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    alerts,
    assignments,
    auth,
    caller,
    dispatch,
    driver,
    events,
    false_alarm,
    health,
    hospitals,
    incidents,
    ledger,
    mci_demand,
    prealerts,
    reference,
    risks,
    routing,
    simulation,
    vehicles,
)
from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db.pg import NotFound, Store
from app.db.seed import seed
from app.services.access_service import AccessError
from app.services.context import Ctx
from app.services.heartbeat import heartbeat_loop, risk_loop, watchdog_loop
from app.services.hospital_service import InvalidResourceUpdate
from app.services.incident_service import RuleViolation
from app.services.vehicle_service import InvalidTransition, InvalidUpdate
from app.websockets.manager import EventHub

ALLOWED_DELETE = re.compile(r"^/api/routing/disruptions/[^/]+$")  # lifting a road closure: audited, not a record deletion


def create_app(settings: Settings | None = None, *, database_url: str | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(database_url or settings.database_url, init_schema=not settings.db_skip_schema)
        await store.open()
        ctx = Ctx(settings=settings, store=store, hub=EventHub())
        app.state.ctx = ctx
        if not await store.get("meta", "seeded") and not await ctx.vehicle_repo.list():
            await seed(ctx)
            await store.put("meta", "seeded", {"id": "seeded"})
        tasks = []
        if background and settings.simulated_heartbeat:
            tasks.append(asyncio.create_task(heartbeat_loop(ctx)))
        if background:
            tasks.append(asyncio.create_task(watchdog_loop(ctx)))
            tasks.append(asyncio.create_task(risk_loop(ctx)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await store.close()

    app = FastAPI(title="ResQGrid", description="Simulated dispatch prototype. Synthetic data only.",
                  lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    routers = (health, auth, incidents, vehicles, hospitals, events, reference, dispatch, routing,
               assignments, caller, false_alarm, driver, prealerts, alerts, risks, mci_demand, simulation, ledger)
    for r in routers:
        app.include_router(r.router)

    @app.middleware("http")
    async def no_deletes(request: Request, call_next):
        """Delete-proofing: nothing in the domain is ever deleted. A DELETE (or PUT) on the API is refused with 405 AND
        written to the ledger as a delete_attempt with the actor. The one exception is lifting a road closure, which is
        itself audited. 'Removal' is a tombstone event with a reason."""
        path = request.url.path
        if request.method in ("DELETE", "PUT") and path.startswith("/api/") and not ALLOWED_DELETE.match(path):
            actor = "anonymous"
            h = request.headers.get("authorization", "")
            if h.lower().startswith("bearer "):
                try:
                    actor = decode_token(h[7:]).sub
                except HTTPException:
                    actor = "invalid-token"
            ctx = getattr(request.app.state, "ctx", None)
            if ctx is not None:
                await ctx.ledger.record_delete_attempt(request.method, path, actor)
            return JSONResponse({"detail": "Nothing is deleted. Use a tombstone with a reason (POST /api/tombstones). This attempt was recorded."},
                                status_code=405, headers={"Allow": "GET, POST, PATCH"})
        return await call_next(request)

    def err(status: int):
        async def handler(_: Request, exc: Exception):
            return JSONResponse({"detail": str(exc)}, status_code=status)

        return handler

    app.add_exception_handler(RuleViolation, err(409))
    app.add_exception_handler(AccessError, err(422))
    app.add_exception_handler(InvalidTransition, err(409))
    app.add_exception_handler(InvalidUpdate, err(422))
    app.add_exception_handler(InvalidResourceUpdate, err(422))
    app.add_exception_handler(NotFound, err(404))
    app.add_exception_handler(KeyError, err(404))
    return app


app = create_app()
