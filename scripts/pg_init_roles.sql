-- M19 delete-proofing at the database. The application should connect as `resqgrid_app`, a role that can only
-- INSERT and SELECT on the ledger tables: no UPDATE, no DELETE, no TRUNCATE, no DROP.
--
--   psql -v app_password='choose-a-password' -d resqgrid -f scripts/pg_init_roles.sql
--   (or: venv\Scripts\python.exe scripts\apply_pg_roles.py, which does the same and reads LEDGER_APP_PASSWORD)
--
-- Run it as the database owner. Then set DATABASE_URL to the resqgrid_app login and DB_SKIP_SCHEMA=true
-- (schema changes need the owner). Resetting the demo (TRUNCATE) also needs the owner: that is the point.
-- The audit_events, actor_keys and ledger_anchors tables ALSO have triggers that refuse UPDATE and DELETE,
-- so even the owner cannot change a row by accident.

do $$
begin
  if not exists (select from pg_roles where rolname = 'resqgrid_app') then
    create role resqgrid_app login;
  end if;
end $$;
alter role resqgrid_app with login password :'app_password';

revoke all on all tables in schema public from resqgrid_app;
grant usage on schema public to resqgrid_app;

-- ledger, audit, anchors, actor keys, nonces: append-only
grant select, insert on audit_events, ledger_anchors, actor_keys, ledger_nonces to resqgrid_app;

-- ordinary application state
grant select, insert, update, delete on docs, processed_updates to resqgrid_app;

-- per-subject encryption keys: UPDATE is how crypto-shredding destroys a key
grant select, insert, update on subject_keys to resqgrid_app;

grant usage, select, update on all sequences in schema public to resqgrid_app;
