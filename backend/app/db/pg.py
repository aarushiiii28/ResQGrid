"""PostgreSQL store (psycopg 3, async pool). Entities are JSONB documents in one table;
the audit log is a real table protected by a trigger so it is append-only."""

from collections.abc import Callable
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
create table if not exists docs (
  kind text not null,
  id text not null,
  data jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (kind, id)
);
create index if not exists docs_kind_idx on docs (kind);
create table if not exists processed_updates (key text primary key, at timestamptz default now());
create table if not exists audit_events (
  seq bigserial primary key,
  event_id text unique not null,
  correlation_id text,
  entity_type text not null,
  entity_id text not null,
  event_type text not null,
  occurred_at timestamptz not null,
  actor text not null,
  source text,
  reason text,
  old_value jsonb,
  new_value jsonb,
  payload jsonb not null default '{}'::jsonb
);
create index if not exists audit_corr_idx on audit_events (correlation_id);
create index if not exists audit_entity_idx on audit_events (entity_type, entity_id);
create or replace function audit_append_only() returns trigger as $$
begin
  raise exception 'audit_events is append-only';
end;
$$ language plpgsql;
drop trigger if exists audit_no_change on audit_events;
create trigger audit_no_change before update or delete on audit_events
  for each row execute function audit_append_only();
-- M19 ledger: audit_events IS the ledger. Each row is hash-chained and signed; rows can never be changed or removed.
alter table audit_events add column if not exists salt text;
alter table audit_events add column if not exists payload_commitment text;
alter table audit_events add column if not exists prev_hash text;
alter table audit_events add column if not exists entry_hash text;
alter table audit_events add column if not exists signature text;
alter table audit_events add column if not exists sig_message text;
alter table audit_events add column if not exists signer_key_id text;
alter table audit_events add column if not exists server_signature text;
alter table audit_events add column if not exists personal_enc jsonb;
alter table audit_events add column if not exists subject_id text;
create index if not exists audit_hash_idx on audit_events (entry_hash);
create table if not exists actor_keys (
  seq bigserial primary key,
  key_id text not null,
  actor_id text not null,
  event text not null check (event in ('registered', 'revoked')),
  public_key text,
  algo text not null default 'ed25519',
  at timestamptz not null default now(),
  reason text,
  unique (key_id, event)
);
create table if not exists ledger_anchors (
  seq bigserial primary key,
  anchor_id text unique not null,
  from_seq bigint not null,
  to_seq bigint not null,
  merkle_root text not null,
  anchored_at timestamptz not null,
  provider text not null,
  reference jsonb not null default '{}'::jsonb
);
create table if not exists ledger_nonces (
  key_id text not null,
  nonce text not null,
  ctr bigint not null,
  at timestamptz not null default now(),
  primary key (key_id, nonce)
);
create index if not exists ledger_nonces_ctr on ledger_nonces (key_id, ctr);
create table if not exists subject_keys (
  subject_id text primary key,
  wrapped_key text,
  created_at timestamptz not null default now(),
  destroyed_at timestamptz
);
create or replace function ledger_append_only() returns trigger as $$
begin
  raise exception '% is append-only', tg_table_name;
end;
$$ language plpgsql;
drop trigger if exists actor_keys_no_change on actor_keys;
create trigger actor_keys_no_change before update or delete on actor_keys
  for each row execute function ledger_append_only();
drop trigger if exists anchors_no_change on ledger_anchors;
create trigger anchors_no_change before update or delete on ledger_anchors
  for each row execute function ledger_append_only();
create sequence if not exists anchor_seq start 1;
create sequence if not exists incident_seq start 1001;
create sequence if not exists report_seq start 1;
create sequence if not exists disruption_seq start 1;
create sequence if not exists access_report_seq start 1;
create sequence if not exists decision_seq start 1;
create sequence if not exists assignment_seq start 1;
create sequence if not exists call_seq start 1;
create sequence if not exists prealert_seq start 1;
create sequence if not exists valert_seq start 1;
create sequence if not exists reroute_seq start 1;
create sequence if not exists risk_seq start 1;
create sequence if not exists mci_seq start 1;
create sequence if not exists simrun_seq start 1;
create sequence if not exists confirm_seq start 1;
"""

AUDIT_COLS = (
    "seq, event_id, correlation_id, entity_type, entity_id, event_type, occurred_at, actor, "
    "source, reason, old_value, new_value, payload, salt, payload_commitment, prev_hash, entry_hash, "
    "signature, sig_message, signer_key_id, server_signature, personal_enc, subject_id"
)


class NotFound(Exception):
    pass


class Store:
    def __init__(self, url: str, *, max_size: int = 8, init_schema: bool = True):
        self.url = url
        self.init_schema = init_schema
        self.pool = AsyncConnectionPool(
            url,
            open=False,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=10)
        if not self.init_schema:  # restricted role: the owner created the schema
            return
        async with self.pool.connection() as conn:
            await conn.execute("select pg_advisory_lock(727272)")
            try:
                await conn.execute(SCHEMA)
            finally:
                await conn.execute("select pg_advisory_unlock(727272)")

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> bool:
        try:
            async with self.pool.connection(timeout=2) as conn:
                await conn.execute("select 1")
            return True
        except Exception:
            return False

    async def truncate_all(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("truncate docs, processed_updates, audit_events, actor_keys, ledger_anchors, ledger_nonces, subject_keys restart identity")
            await conn.execute("alter sequence incident_seq restart with 1001")
            for seq in ("report_seq", "disruption_seq", "access_report_seq", "decision_seq", "assignment_seq", "call_seq", "prealert_seq", "valert_seq", "reroute_seq", "risk_seq", "mci_seq", "simrun_seq", "confirm_seq", "anchor_seq"):
                await conn.execute(f"alter sequence {seq} restart with 1")

    async def next_seq(self, name: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"select nextval('{name}') as v")  # name is internal constant
            return (await cur.fetchone())["v"]

    # ---- documents -------------------------------------------------
    async def get(self, kind: str, id_: str) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select data from docs where kind=%s and id=%s", (kind, id_))
            row = await cur.fetchone()
            return row["data"] if row else None

    async def put(self, kind: str, id_: str, data: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into docs (kind, id, data) values (%s,%s,%s) "
                "on conflict (kind,id) do update set data=excluded.data, updated_at=now()",
                (kind, id_, Jsonb(data)),
            )

    async def list(self, kind: str) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select data from docs where kind=%s order by id", (kind,))
            return [r["data"] for r in await cur.fetchall()]

    async def delete(self, kind: str, id_: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("delete from docs where kind=%s and id=%s", (kind, id_))

    async def modify(
        self,
        kind: str,
        id_: str,
        fn: Callable[[dict], dict | None],
        *,
        claim_key: str | None = None,
    ) -> tuple[dict | None, str]:
        """Locked read-modify-write. Returns (doc, outcome).
        outcome: applied | noop | duplicate. If `claim_key` was already processed the
        change is skipped (idempotency). If fn raises, everything rolls back."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "select data from docs where kind=%s and id=%s for update", (kind, id_)
                )
                row = await cur.fetchone()
                if row is None:
                    raise NotFound(f"{kind}:{id_}")
                if claim_key is not None:
                    cur = await conn.execute(
                        "insert into processed_updates (key) values (%s) "
                        "on conflict do nothing returning key",
                        (claim_key,),
                    )
                    if await cur.fetchone() is None:
                        return row["data"], "duplicate"
                new = fn(row["data"])
                if new is None:
                    return row["data"], "noop"
                await conn.execute(
                    "update docs set data=%s, updated_at=now() where kind=%s and id=%s",
                    (Jsonb(new), kind, id_),
                )
                return new, "applied"

    # ---- audit / ledger -----------------------------------------------
    async def append_chained(self, make: Callable[[int, str], dict[str, Any]]) -> dict:
        """Append ONE ledger entry. Under a transaction-level advisory lock the last (seq, entry_hash) is read,
        `make(seq, prev_hash)` builds the signed entry, and it is inserted: appends are strictly sequential and
        gap-free even under concurrency, and the chain continues correctly after a restart."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("select pg_advisory_xact_lock(727301)")
                cur = await conn.execute("select seq, entry_hash from audit_events order by seq desc limit 1")
                last = await cur.fetchone()
                seq = (last["seq"] + 1) if last else 1
                prev = last["entry_hash"] if last and last["entry_hash"] else "0" * 64
                e = make(seq, prev)
                cur = await conn.execute(
                    "insert into audit_events (seq, event_id, correlation_id, entity_type, entity_id, "
                    "event_type, occurred_at, actor, source, reason, old_value, new_value, payload, salt, "
                    "payload_commitment, prev_hash, entry_hash, signature, sig_message, signer_key_id, "
                    "server_signature, personal_enc, subject_id) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning " + AUDIT_COLS,
                    (
                        seq, e["event_id"], e.get("correlation_id"), e["entity_type"], e["entity_id"], e["event_type"],
                        e["occurred_at"], e["actor"], e.get("source"), e.get("reason"),
                        Jsonb(e.get("old_value")), Jsonb(e.get("new_value")), Jsonb(e.get("payload") or {}),
                        e["salt"], e["payload_commitment"], e["prev_hash"], e["entry_hash"], e.get("signature"),
                        e.get("sig_message"), e["signer_key_id"], e.get("server_signature"),
                        Jsonb(e["personal_enc"]) if e.get("personal_enc") is not None else None, e.get("subject_id"),
                    ),
                )
                return await cur.fetchone()

    async def ledger_rows(self, *, from_seq: int = 1, to_seq: int | None = None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"select {AUDIT_COLS} from audit_events where seq >= %s and seq <= %s order by seq",
                (from_seq, to_seq if to_seq is not None else 2**62),
            )
            return await cur.fetchall()

    async def ledger_row(self, event_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"select {AUDIT_COLS} from audit_events where event_id=%s", (event_id,))
            return await cur.fetchone()

    async def ledger_head(self) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select seq, entry_hash, occurred_at from audit_events order by seq desc limit 1")
            return await cur.fetchone()

    # actor keys (append-only: registration and revocation are separate events)
    async def add_key_event(self, key_id: str, actor_id: str, event: str, public_key: str | None, reason: str | None = None) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "insert into actor_keys (key_id, actor_id, event, public_key, reason) values (%s,%s,%s,%s,%s) "
                "on conflict (key_id, event) do nothing returning seq", (key_id, actor_id, event, public_key, reason))
            return await cur.fetchone() is not None

    async def key_events(self, *, actor_id: str | None = None, key_id: str | None = None) -> list[dict]:
        where, args = ["true"], []
        if actor_id:
            where.append("actor_id = %s")
            args.append(actor_id)
        if key_id:
            where.append("key_id = %s")
            args.append(key_id)
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"select seq, key_id, actor_id, event, public_key, algo, at, reason from actor_keys where {' and '.join(where)} order by seq", args)
            return await cur.fetchall()

    async def claim_nonce(self, key_id: str, nonce: str, ctr: int) -> str:
        """'ok', 'replay' (nonce seen) or 'stale' (counter not above the last one). Atomic per key."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (key_id,))
                cur = await conn.execute("select 1 from ledger_nonces where key_id=%s and nonce=%s", (key_id, nonce))
                if await cur.fetchone():
                    return "replay"
                cur = await conn.execute("select coalesce(max(ctr), -1) as m from ledger_nonces where key_id=%s", (key_id,))
                if ctr <= (await cur.fetchone())["m"]:
                    return "stale"
                await conn.execute("insert into ledger_nonces (key_id, nonce, ctr) values (%s,%s,%s)", (key_id, nonce, ctr))
                return "ok"

    # anchors (append-only)
    async def add_anchor(self, a: dict) -> dict:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "insert into ledger_anchors (anchor_id, from_seq, to_seq, merkle_root, anchored_at, provider, reference) "
                "values (%s,%s,%s,%s,%s,%s,%s) returning anchor_id, from_seq, to_seq, merkle_root, anchored_at, provider, reference",
                (a["anchor_id"], a["from_seq"], a["to_seq"], a["merkle_root"], a["anchored_at"], a["provider"], Jsonb(a.get("reference") or {})))
            return await cur.fetchone()

    async def list_anchors(self) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select anchor_id, from_seq, to_seq, merkle_root, anchored_at, provider, reference from ledger_anchors order by seq")
            return await cur.fetchall()

    # per-subject encryption keys (the only ledger-adjacent table that may be changed: shredding destroys a key)
    async def get_subject_key(self, subject_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select subject_id, wrapped_key, created_at, destroyed_at from subject_keys where subject_id=%s", (subject_id,))
            return await cur.fetchone()

    async def create_subject_key(self, subject_id: str, wrapped: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("insert into subject_keys (subject_id, wrapped_key) values (%s,%s) on conflict do nothing", (subject_id, wrapped))

    async def destroy_subject_key(self, subject_id: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("update subject_keys set wrapped_key = null, destroyed_at = now() where subject_id=%s and wrapped_key is not null returning 1", (subject_id,))
            return await cur.fetchone() is not None

    async def list_audit(
        self,
        *,
        correlation_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict]:
        where, args = ["seq > %s"], [after_seq]
        if correlation_id:
            where.append("correlation_id = %s")
            args.append(correlation_id)
        if entity_type:
            where.append("entity_type = %s")
            args.append(entity_type)
        if entity_id:
            where.append("entity_id = %s")
            args.append(entity_id)
        args.append(limit)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"select {AUDIT_COLS} from audit_events where {' and '.join(where)} "
                "order by seq limit %s",
                args,
            )
            return await cur.fetchall()
