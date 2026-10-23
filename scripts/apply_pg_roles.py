"""Create the append-only application role (see scripts/pg_init_roles.sql).

    python scripts/apply_pg_roles.py [--database-url URL]

Reads LEDGER_APP_PASSWORD (default: a demo password, printed). Run as the database owner."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

import app  # noqa: E402,F401
from app.core.config import get_settings  # noqa: E402

SQL_FILE = Path(__file__).with_name("pg_init_roles.sql")
DEMO_PASSWORD = "resqgrid-app-demo"


def apply(url: str, password: str) -> None:
    text = SQL_FILE.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    body = body.replace(":'app_password'", sql.Literal(password).as_string())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(body)  # one simple-query call: the DO block and the grants run in order


def app_url(url: str, password: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(url)
    info.update(user="resqgrid_app", password=password)
    return psycopg.conninfo.make_conninfo(**info)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url")
    a = ap.parse_args()
    url = a.database_url or get_settings().database_url
    pw = os.environ.get("LEDGER_APP_PASSWORD", DEMO_PASSWORD)
    apply(url, pw)
    print("role resqgrid_app ready: INSERT and SELECT only on audit_events, ledger_anchors, actor_keys, ledger_nonces.")
    print("point the app at it with DATABASE_URL (login resqgrid_app) and DB_SKIP_SCHEMA=true")
    if pw == DEMO_PASSWORD:
        print("using the DEMO password; set LEDGER_APP_PASSWORD for anything else")


if __name__ == "__main__":
    main()
