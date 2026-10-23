import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import get_ctx
from app.core.security import Principal, require
from app.services.context import Ctx
from app.services.simulation_service import BenchmarkRequest, RunRequest, SimRun
from app.simulation.false_alarm_lab import run_mix
from app.simulation.model import ALL_FAULTS
from app.simulation.route_lab import run_compare as run_route_lab

router = APIRouter(prefix="/api/simulation", tags=["simulation"])
OPS = ("dispatcher", "admin")


@router.get("/faults")
async def faults(_: Principal = Depends(require(*OPS))):
    return {"faults": list(ALL_FAULTS), "policies": {"P0": "baseline", "P1": "optimized", "P2": "ResQGrid"},
            "label": "SIMULATED: synthetic world, not real emergencies"}


@router.post("/runs", response_model=SimRun, status_code=201)
async def create_run(body: RunRequest, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Run the chosen policies on one seeded world and save seed, config, policy versions, timeline and metrics."""
    try:
        return await ctx.simulation.run(body, p.sub)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/runs")
async def list_runs(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.simulation.list()


@router.get("/runs/{run_id}", response_model=SimRun)
async def get_run(run_id: str, _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    try:
        return await ctx.simulation.get(run_id)
    except KeyError as e:
        raise HTTPException(404, "run not found") from e


@router.get("/runs/{run_id}/export")
async def export_run(run_id: str, format: str = Query("json", pattern="^(json|csv)$"),
                     part: str = Query("metrics", pattern="^(metrics|incidents|timeline)$"),
                     _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    try:
        run = await ctx.simulation.get(run_id)
    except KeyError as e:
        raise HTTPException(404, "run not found") from e
    body, media = ctx.simulation.export(run, format, part)
    name = f"{run_id}-{part if format == 'csv' else 'full'}.{format}"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


class FalseAlarmMixRequest(BaseModel):
    seed: int = Field(default=42, ge=0, le=10_000_000)
    n: int = Field(default=300, ge=20, le=3000)


@router.post("/false-alarm")
async def false_alarm_mix(body: FalseAlarmMixRequest, _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """M18: seeded synthetic call mix (genuine, child taps, pocket and test presses, abandoned) through the live model.
    Reports flagged false alarms, MISSED genuine emergencies in the LOW lane, time to verify, workload and a sweep."""
    cfg = await ctx.false_alarm.cfg()
    return await asyncio.to_thread(run_mix, body.seed, body.n, cfg)


class RouteLabRequest(BaseModel):
    seed: int = Field(default=1, ge=0, le=10_000_000)
    n: int = Field(default=8, ge=2, le=24)
    sweep: bool = True


@router.post("/routes")
async def route_lab(body: RouteLabRequest, _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """M20: identical seeded trips under reroute-on-every-change, never-reroute and the ResQGrid stable policy, with a
    threshold sweep and the places where the stable policy is worse. Simulated."""
    return await asyncio.to_thread(run_route_lab, body.seed, body.n, ctx.stability.policy, body.sweep)


@router.post("/benchmark")
async def run_benchmark(body: BenchmarkRequest, _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Many seeds, same worlds for every policy: distributions, paired wins and losses, and where ResQGrid is worse."""
    try:
        return await ctx.simulation.benchmark(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
