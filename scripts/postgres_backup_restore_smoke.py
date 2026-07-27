#!/usr/bin/env python3
"""Run a disposable PostgreSQL backup/restore verification rehearsal."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postgres_backup import create_backup, sanitize_text
from scripts.postgres_restore_verify import restore_and_verify


RESTORE_DATABASE_PREFIX = "teleroute_restore_verify_"


def restore_database_name() -> str:
    return RESTORE_DATABASE_PREFIX + uuid.uuid4().hex[:12]


def database_url_with_name(database_url: str, name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))


def create_database(admin_url: str, name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(admin_url: str, name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def verify_restored_database(database_url: str) -> None:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("restored database SELECT 1 verification failed")
            cursor.execute("SELECT COUNT(*) FROM users")
            if int(cursor.fetchone()[0]) < 1:
                raise RuntimeError("restored users table is empty")


def run_smoke(database_url: str, workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    backup_file = workdir / "teleroute-backup.dump"
    manifest_file = workdir / "teleroute-backup.manifest.json"
    name = restore_database_name()
    admin_url = database_url_with_name(database_url, "postgres")
    restore_url = database_url_with_name(database_url, name)
    dropped = False
    try:
        create_backup(database_url, backup_file, manifest_file, overwrite=True)
        create_database(admin_url, name)
        restore_and_verify(backup_file, restore_url, manifest_file)
        verify_restored_database(restore_url)
    finally:
        drop_database(admin_url, name)
        dropped = True
    return {
        "backup_file": str(backup_file),
        "manifest_file": str(manifest_file),
        "restore_database": name,
        "restored_database_dropped": dropped,
        "status": "ok",
        "table_counts_verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.database_url, args.workdir)
    except Exception as exc:
        print(sanitize_text(str(exc), args.database_url), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
