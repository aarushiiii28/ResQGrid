from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api.deps import get_ctx
from app.core.security import Principal, decode_token, require
from app.services.audit_service import AuditService
from app.services.context import Ctx

router = APIRouter()


@router.get("/api/events", tags=["events"])
async def list_events(
    correlation_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    format: str = Query("json", pattern="^(json|csv)$"),
    p: Principal = Depends(require("dispatcher", "admin")),
    ctx: Ctx = Depends(get_ctx),
):
    events = await ctx.audit.list(correlation_id=correlation_id, entity_type=entity_type,
                                  entity_id=entity_id, after_seq=after_seq, limit=limit)
    if format == "csv":
        from fastapi import Response

        return Response(AuditService.to_csv(events), media_type="text/csv")
    return JSONResponse([e.model_dump(mode="json") for e in events])


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket, token: str = Query(...), since: int | None = Query(None)):
    """Envelope: event_id, event_type, occurred_at, entity_id, correlation_id, source, payload, seq.
    On reconnect pass `since=<last seq>`; on `sync.resync_required` reload REST state."""
    try:
        principal = decode_token(token)
    except Exception:
        await ws.close(code=4401)
        return
    ctx: Ctx = ws.app.state.ctx
    try:
        await ctx.hub.serve(ws, principal, since)
    except WebSocketDisconnect:
        pass
