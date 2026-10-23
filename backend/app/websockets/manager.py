"""Realtime hub. Every event gets a monotonically increasing `seq`. Clients that reconnect
pass `since=<last seq>`; if the buffer no longer covers it they receive
`sync.resync_required` and must reload REST state."""

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.core.security import Principal
from app.schemas.common import utcnow


def visible_to(p: Principal, vis: dict[str, Any] | None) -> bool:
    if p.role in ("dispatcher", "admin"):
        return True
    vis = vis or {}
    if p.role == "caller":
        return vis.get("caller") == p.sub
    if p.role == "driver":
        return p.vehicle_id is not None and vis.get("vehicle_id") == p.vehicle_id
    if p.role == "hospital":
        return p.hospital_id is not None and vis.get("hospital_id") == p.hospital_id
    return False


@dataclass(eq=False)
class Client:
    ws: WebSocket
    principal: Principal
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))


class EventHub:
    def __init__(self, buffer_size: int = 2000):
        self.seq = 0
        self.buffer: deque[tuple[dict, dict | None]] = deque(maxlen=buffer_size)
        self.clients: set[Client] = set()

    def publish(
        self,
        event_type: str,
        entity_id: str,
        *,
        correlation_id: str | None = None,
        source: str = "system",
        payload: dict | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        vis: dict | None = None,
    ) -> dict:
        self.seq += 1
        env = {
            "event_id": event_id or str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": occurred_at or utcnow().isoformat(),
            "entity_id": entity_id,
            "correlation_id": correlation_id,
            "source": source,
            "payload": payload or {},
            "seq": self.seq,
        }
        self.buffer.append((env, vis))
        for c in list(self.clients):
            if visible_to(c.principal, vis):
                try:
                    c.queue.put_nowait(env)
                except asyncio.QueueFull:
                    self.clients.discard(c)
        return env

    def replay(self, p: Principal, since: int) -> list[dict] | None:
        """Events after `since` visible to p, or None if the buffer cannot cover the gap."""
        if self.buffer and since < self.buffer[0][0]["seq"] - 1:
            return None
        return [e for e, v in self.buffer if e["seq"] > since and visible_to(p, v)]

    async def serve(self, ws: WebSocket, principal: Principal, since: int | None) -> None:
        await ws.accept()
        client = Client(ws, principal)
        hello = {
            "event_id": str(uuid.uuid4()),
            "event_type": "sync.hello",
            "occurred_at": utcnow().isoformat(),
            "entity_id": principal.sub,
            "correlation_id": None,
            "source": "system",
            "payload": {"seq": self.seq, "role": principal.role},
            "seq": self.seq,
        }
        await ws.send_json(hello)
        self.clients.add(client)  # register first so nothing is lost during replay
        last = since if since is not None else self.seq
        try:
            if since is not None:
                missed = self.replay(principal, since)
                if missed is None:
                    await ws.send_json({**hello, "event_type": "sync.resync_required"})
                    last = self.seq
                else:
                    for e in missed:
                        await ws.send_json(e)
                        last = max(last, e["seq"])
            while True:
                env = await client.queue.get()
                if env["seq"] > last:
                    await ws.send_json(env)
                    last = env["seq"]
        finally:
            self.clients.discard(client)
