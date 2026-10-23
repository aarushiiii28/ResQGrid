import csv
import io
import json
import uuid
from typing import Any

from app.db.pg import Store
from app.schemas.audit import AuditEvent, LedgerInfo
from app.schemas.common import utcnow
from app.services.ledger_service import PERSONAL_EVENTS, PERSONAL_KEYS, PERSONAL_PLACEHOLDER
from app.websockets.manager import EventHub

CSV_COLUMNS = [
    "seq",
    "event_id",
    "correlation_id",
    "entity_type",
    "entity_id",
    "event_type",
    "occurred_at",
    "actor",
    "source",
    "reason",
    "old_value",
    "new_value",
    "payload",
    "entry_hash",
    "ledger_status",
]


class AuditService:
    """Every audit event is written THROUGH the ledger (hash-chained, signed, anchored later). The M10 timeline and
    exports read the same rows, with personal values decrypted (or '[erased]' after crypto-shredding)."""

    def __init__(self, store: Store, hub: EventHub, now=utcnow):
        self.store, self.hub, self.now = store, hub, now
        self.ledger = None  # LedgerService, set by Ctx

    async def record(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str,
        correlation_id: str | None = None,
        source: str | None = None,
        reason: str | None = None,
        old: Any = None,
        new: Any = None,
        payload: dict | None = None,
        vis: dict | None = None,
    ) -> AuditEvent:
        event = {
            "event_id": str(uuid.uuid4()),
            "correlation_id": correlation_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "occurred_at": self.now(),
            "actor": actor,
            "source": source,
            "reason": reason,
            "old_value": old,
            "new_value": new,
            "payload": payload or {},
        }
        personal, subject = None, None
        if event_type in PERSONAL_EVENTS and vis and vis.get("caller"):
            subject, personal = vis["caller"], {}
            for part in ("old_value", "new_value", "payload"):
                d = event[part]
                moved = {k: d[k] for k in PERSONAL_KEYS if isinstance(d, dict) and k in d}
                if moved:
                    personal[part] = moved
                    event[part] = {**d, **{k: PERSONAL_PLACEHOLDER for k in moved}}
        row = await self.ledger.append(event, personal=personal or None, subject_id=subject)
        ev = AuditEvent.model_validate({**row, "old_value": old, "new_value": new, "payload": payload or {}})
        self.hub.publish(
            event_type,
            entity_id,
            correlation_id=correlation_id,
            source=source or "system",
            event_id=ev.event_id,
            occurred_at=ev.occurred_at.isoformat(),
            payload={
                "actor": actor,
                "reason": reason,
                "old": old,
                "new": new,
                **(payload or {}),
            },
            vis=vis,
        )
        return ev

    async def list(self, **filters) -> list[AuditEvent]:
        rows = await self.store.list_audit(**filters)
        anchors = await self.store.list_anchors()
        out = []
        for r in rows:
            r = await self.ledger.hydrate(r)
            ev = AuditEvent.model_validate(r)
            ev.ledger = LedgerInfo(**self.ledger.status_of(r, anchors))
            out.append(ev)
        return out

    @staticmethod
    def to_json(events: list[AuditEvent]) -> str:
        return json.dumps([e.model_dump(mode="json") for e in events], indent=2)

    @staticmethod
    def to_csv(events: list[AuditEvent]) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for e in events:
            d = e.model_dump(mode="json")
            for k in ("old_value", "new_value", "payload"):
                d[k] = json.dumps(d[k], sort_keys=True)
            led = d.get("ledger") or {}
            d["entry_hash"], d["ledger_status"] = led.get("entry_hash"), led.get("ledger_status")
            w.writerow({c: d.get(c) for c in CSV_COLUMNS})
        return buf.getvalue()
