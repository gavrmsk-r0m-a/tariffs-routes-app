#!/usr/bin/env python3
"""Create the TeleRoute schema and a deliberately local development user."""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_postgres  # noqa: E402
from app.repository import hash_password  # noqa: E402

SCHEMA_PATH = ROOT / "docs/postgres/schema.postgres.sql"
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
DEFAULT_USERNAME = "local-dev"


def validate_local_database_url(database_url: str) -> None:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("database URL must use the postgres or postgresql scheme")
    if parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("refusing non-local PostgreSQL host; use localhost or 127.0.0.1")
    if not parsed.path or parsed.path == "/":
        raise ValueError("database URL must name a local database")


def setup_database(database_url: str, username: str, password: str) -> None:
    validate_local_database_url(database_url)
    password_hash, password_salt = hash_password(password)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect_postgres(database_url)
    try:
        conn.execute(schema)
        conn.execute(
            """
            INSERT INTO users(username, display_name, role_key, role, must_change_password,
                              is_active, password_hash, password_salt)
            VALUES (%s, %s, 'admin', 'admin', false, true, %s, %s)
            ON CONFLICT(username) DO UPDATE SET
                display_name = excluded.display_name, role_key = excluded.role_key,
                role = excluded.role, must_change_password = false, is_active = true,
                password_hash = excluded.password_hash, password_salt = excluded.password_salt,
                updated_at = CURRENT_TIMESTAMP
            """,
            (username, "Local development admin", password_hash, password_salt),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", help="local login password (generated when omitted)")
    args = parser.parse_args(argv)
    password = args.password or secrets.token_urlsafe(18)
    try:
        setup_database(args.database_url, args.username, password)
    except Exception as exc:
        # Never echo a connection URL: driver messages can otherwise disclose its password.
        database_password = unquote(urlsplit(args.database_url).password or "")
        message = str(exc).replace(args.database_url, "[local database URL]")
        if database_password:
            message = message.replace(database_password, "***")
        print(f"Local PostgreSQL setup failed: {message}", file=sys.stderr)
        return 1
    print("Local PostgreSQL database is ready.")
    print(f"Login: {args.username}")
    print(f"Password: {password}")
    print("These credentials are local-only; do not reuse them elsewhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

