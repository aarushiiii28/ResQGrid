"""M11 Replay Lab service: runs the deterministic simulator, saves runs, exports them."""

import asyncio
import csv
import io
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.repos import Repo
from app.simulation import metrics as mx
from app.simulation.model import ALL_FAULTS
from app.simulation.runner import LABEL, benchmark, run_compare


class RunRequest(BaseModel):
    seed: int = Field(default=42, ge=0, le=10_000_000)
    policies: list[str] = ["P0", "P1", "P2"]
    faults: list[str] | None = None  # None = every fault
    n_incidents: int | None = Field(default=None, ge=4, le=40)


class BenchmarkRequest(BaseModel):
    seeds: int = Field(default=10, ge=2, le=40)
    first_seed: int = Field(default=1, ge=0)
    faults: list[str] | None = None
    policies: list[str] = ["P0", "P1", "P2"]


class SimRun(BaseModel):
    id: str
    created_at: datetime
    created_by: str
    label: str
    seed: int
    faults: list[str]
    request: dict[str, Any]
    config: dict[str, Any]
    policy_versions: dict[str, str]
    world: dict[str, Any]
    results: dict[str, Any]


class SimulationService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.repo: Repo[SimRun] = Repo(ctx.store, "simrun", SimRun)

    @staticmethod
    def validate(policies: list[str], faults: list[str] | None) -> None:
        bad = [p for p in policies if p not in ("P0", "P1", "P2")]
        if bad or not policies:
            raise ValueError(f"policies must be a non-empty subset of P0, P1, P2 (got {policies})")
        badf = [f for f in (faults or []) if f not in ALL_FAULTS]
        if badf:
            raise ValueError(f"unknown fault(s): {', '.join(badf)}. Known: {', '.join(ALL_FAULTS)}")

    async def run(self, req: RunRequest, actor: str) -> SimRun:
        self.validate(req.policies, req.faults)
        over = {} if req.n_incidents is None else {"n_incidents": req.n_incidents}
        out = await asyncio.to_thread(run_compare, req.seed, req.faults, req.policies, **over)
        n = await self.ctx.store.next_seq("simrun_seq")
        run = SimRun(id=f"SIM-{n:04d}", created_at=self.ctx.now(), created_by=actor, label=out["label"], seed=req.seed,
                     faults=out["faults"], request=req.model_dump(), config=out["config"],
                     policy_versions={p: r["policy_version"] for p, r in out["results"].items()}, world=out["world"],
                     results=out["results"])
        await self.repo.put(run)
        await self.ctx.audit.record(entity_type="simulation", entity_id=run.id, event_type="simulation.run_saved", actor=actor,
                                    source="simulated", payload={"seed": req.seed, "faults": run.faults, "policies": req.policies,
                                                                 "policy_versions": run.policy_versions})
        return run

    async def get(self, run_id: str) -> SimRun:
        r = await self.repo.get(run_id)
        if r is None:
            raise KeyError(run_id)
        return r

    async def list(self) -> list[dict[str, Any]]:
        return [{"id": r.id, "created_at": r.created_at, "seed": r.seed, "faults": r.faults, "policies": list(r.results),
                 "label": r.label} for r in await self.repo.list()]

    async def benchmark(self, req: BenchmarkRequest) -> dict[str, Any]:
        self.validate(req.policies, req.faults)
        seeds = list(range(req.first_seed, req.first_seed + req.seeds))
        return await asyncio.to_thread(benchmark, seeds, req.faults, req.policies)

    @staticmethod
    def export(run: SimRun, fmt: str, part: str) -> tuple[str, str]:
        if fmt == "json":
            return json.dumps(run.model_dump(mode="json"), indent=1), "application/json"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["# " + LABEL, f"run={run.id}", f"seed={run.seed}", f"faults={'|'.join(run.faults)}"])
        if part == "incidents":
            w.writerow(["policy", "policy_version", "incident", "category", "priority", "t_call_s", "served", "response_s", "dest_s", "meets_requirements"])
            for p, r in run.results.items():
                for i in r["incidents"]:
                    w.writerow([p, r["policy_version"], i["incident"], i["category"], i["priority"], i["t_call"], i["served"], i["response_s"], i["dest_s"], i["meets_requirements"]])
        elif part == "timeline":
            w.writerow(["policy", "policy_version", "t_s", "kind", "incident", "detail"])
            for p, r in run.results.items():
                for e in r["timeline"]:
                    w.writerow([p, r["policy_version"], e["t"], e["kind"], e["incident"], e["detail"]])
        else:
            w.writerow(["policy", "policy_version", "metric", "label", "value"])
            for p, r in run.results.items():
                for k in mx.KEY_METRICS:
                    w.writerow([p, r["policy_version"], k, mx.LABELS[k], r["metrics"].get(k)])
        return buf.getvalue(), "text/csv"
