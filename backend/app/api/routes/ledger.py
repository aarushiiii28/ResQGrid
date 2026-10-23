import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_ctx
from app.core.security import Principal, get_principal, require
from app.services.context import Ctx

router = APIRouter(tags=["ledger"])
OPS = ("dispatcher", "admin")


class KeyBody(BaseModel):
    public_key: str = Field(min_length=40, max_length=64)  # base64 of 32 bytes


class RotateBody(KeyBody):
    old_key_id: str | None = None


class ReasonBody(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class TamperBody(BaseModel):
    mode: Literal["modify", "delete", "reorder", "insert", "truncate"]


class TombstoneBody(BaseModel):
    entity_type: Literal["incident", "report", "vehicle", "hospital"]
    entity_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=3, max_length=300)


# ---- keys (the server stores PUBLIC keys only; private keys never leave the device) ------------------------
@router.post("/api/keys/register", status_code=201)
async def register_key(body: KeyBody, p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.register_key(p.sub, body.public_key, p.sub)


@router.post("/api/keys/rotate", status_code=201)
async def rotate_key(body: RotateBody, p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.rotate_key(p.sub, body.public_key, body.old_key_id, p.sub)


@router.get("/api/keys/me")
async def my_keys(p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.list_keys(p.sub)


@router.get("/api/keys")
async def all_keys(_: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.list_keys()


@router.post("/api/keys/{key_id}/revoke")
async def revoke_key(key_id: str, body: ReasonBody, p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    mine = {k["key_id"] for k in await ctx.ledger.list_keys(p.sub)}
    if p.role != "admin" and key_id not in mine:
        raise HTTPException(403, "you can only revoke your own keys")
    return await ctx.ledger.revoke_key(key_id, p.sub, body.reason)


@router.get("/api/ledger/server-key")
async def server_key(_: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return {"key_id": ctx.ledger.server.key_id, "public_key": ctx.ledger.server.public_b64, "algo": "ed25519"}


# ---- ledger ------------------------------------------------------------------------------------------------
@router.get("/api/ledger/head")
async def head(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    h = await ctx.store.ledger_head()
    anchors = await ctx.ledger.anchors()
    return {"entries": h["seq"] if h else 0, "head_hash": h["entry_hash"] if h else None, "anchors": len(anchors),
            "last_anchor": anchors[-1] if anchors else None, "provider": ctx.ledger.provider.name,
            "provider_label": ctx.ledger.provider.label, "anchor_every_n": ctx.settings.ledger_anchor_every_n}


@router.get("/api/ledger/anchors")
async def anchors(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.anchors()


@router.post("/api/ledger/anchor")
async def anchor_now(p: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    a = await ctx.ledger.anchor_now(actor=p.sub)
    return a or {"anchored": False, "note": "nothing new to anchor"}


@router.get("/api/ledger/proof/{event_id}")
async def proof(event_id: str, _: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.proof(event_id)


@router.get("/api/ledger/verify/{event_id}")
async def verify_event(event_id: str, _: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.verify_event(event_id)


@router.post("/api/ledger/verify")
async def verify_all(_: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Full verification with streaming progress: one JSON object per line (NDJSON), ending with the result."""

    async def gen():
        async for ev in ctx.ledger.verify_all():
            yield json.dumps(ev, default=str) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/api/ledger/tamper-demo")
async def tamper_demo(body: TamperBody, _: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """Demo only: tamper with a throw-away COPY of the chain in memory and show that it is detected."""
    return await ctx.ledger.tamper_demo(body.mode)


# ---- privacy and removal ---------------------------------------------------------------------------------------
@router.post("/api/privacy/erase/{subject_id}")
async def erase(subject_id: str, body: ReasonBody, p: Principal = Depends(require("admin")), ctx: Ctx = Depends(get_ctx)):
    return await ctx.ledger.erase_subject(subject_id, p.sub, body.reason)


@router.post("/api/tombstones", status_code=201)
async def tombstone(body: TombstoneBody, p: Principal = Depends(require(*OPS)), ctx: Ctx = Depends(get_ctx)):
    """'Removal' is a tombstone event with a reason. The record itself is kept."""
    return await ctx.ledger.tombstone(body.entity_type, body.entity_id, body.reason, p.sub)
