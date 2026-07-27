#!/usr/bin/env python3
"""Exercise the real guarded app.db PostgreSQL connection path."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.security import validate_auth_secret  # noqa: E402
from scripts.postgres_backup import sanitize_database_url, sanitize_text  # noqa: E402

EXPECTED_MINIMUM_PUBLIC_TABLES = 1


def _scalar(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def run_smoke(database_url: str, auth_secret: str) -> dict:
    environ = {
        "DB_BACKEND": "postgres",
        "POSTGRES_RUNTIME_ENABLED": "1",
        "DATABASE_URL": database_url,
        "MVP_PRODUCTION_SECURITY": "1",
        "MVP_AUTH_SECRET": auth_secret,
    }
    errors = validate_auth_secret(environ)
    if errors:
        raise ValueError("invalid CI auth secret: " + "; ".join(errors))
    config = db.load_db_config(environ)
    conn = db.connect_database(config, environ)
    try:
        if _scalar(conn.execute("SELECT 1").fetchone()) != 1:
            raise RuntimeError("SELECT 1 failed")
        users_table = _scalar(conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='users')"
        ).fetchone())
        if not users_table:
            raise RuntimeError("users table does not exist")
        users_count = _scalar(conn.execute("SELECT COUNT(*) FROM users").fetchone())
        table_count = _scalar(conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
        ).fetchone())
        if users_count < 1:
            raise RuntimeError("users table must contain at least one row")
        if table_count < EXPECTED_MINIMUM_PUBLIC_TABLES:
            raise RuntimeError("public table count is below expected minimum")
    finally:
        conn.close()
    return {
        "status": "ok", "backend": config.backend, "runtime_guard_enabled": True,
        "users_count": users_count, "table_count": table_count,
        "database_url_sanitized": sanitize_database_url(database_url),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--auth-secret", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.database_url, args.auth_secret)
    except Exception as exc:
        message = sanitize_text(str(exc), args.database_url).replace(args.auth_secret, "***")
        print(json.dumps({"status": "failed", "error": message}) if args.format == "json" else message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "PostgreSQL runtime enablement smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
