from datetime import datetime
from typing import Any

from pydantic import BaseModel


class LedgerInfo(BaseModel):
    """Where this event sits in the tamper-evident ledger (M19)."""

    seq: int | None = None
    entry_hash: str | None = None
    signer_key_id: str | None = None
    client_signed: bool = False
    anchor_id: str | None = None
    ledger_status: str = "PENDING"  # ANCHORED | SIGNED | PENDING


class AuditEvent(BaseModel):
    event_id: str
    correlation_id: str | None
    entity_type: str
    entity_id: str
    event_type: str
    occurred_at: datetime
    actor: str
    source: str | None = None
    reason: str | None = None
    old_value: Any = None
    new_value: Any = None
    payload: dict[str, Any] = {}
    seq: int | None = None
    ledger: LedgerInfo | None = None
