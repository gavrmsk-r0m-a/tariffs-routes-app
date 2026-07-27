#!/usr/bin/env python3
"""Rehearse deployment rollback by restoring into a disposable database."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postgres_backup import create_backup, sanitize_database_url, sanitize_text
from scripts.postgres_deployment_rollback_check import check_rollback_artifacts
from scripts.postgres_restore_verify import restore_and_verify

ROLLBACK_DATABASE_PREFIX = "teleroute_deployment_rollback_"


def sanitize_error(message: str, database_url: str) -> str:
    sanitized = sanitize_text(message, database_url)
    auth_secret = os.environ.get("MVP_AUTH_SECRET", "")
    return sanitized.replace(auth_secret, "***") if auth_secret else sanitized


def rollback_database_name() -> str:
    return ROLLBACK_DATABASE_PREFIX + uuid.uuid4().hex[:12]


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


def verify_rollback_database(database_url: str) -> None:
    import psycopg
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("rollback database SELECT 1 verification failed")
            cursor.execute("SELECT COUNT(*) FROM users")
            if int(cursor.fetchone()[0]) < 1:
                raise RuntimeError("rollback database users table is empty")


def run_smoke(
    database_url: str,
    workdir: Path,
    current_release_sha: str,
    rollback_release_sha: str,
) -> dict:
    """Back up the source and restore it into a fresh database, always removing it."""
    workdir.mkdir(parents=True, exist_ok=True)
    backup_file = workdir / "pre-deployment.dump"
    manifest_file = workdir / "pre-deployment.manifest.json"
    name = rollback_database_name()
    admin_url = database_url_with_name(database_url, "postgres")
    rollback_url = database_url_with_name(database_url, name)
    dropped = False
    try:
        create_backup(database_url, backup_file, manifest_file, overwrite=True)
        check_rollback_artifacts(current_release_sha, rollback_release_sha, manifest_file, strict=True)
        create_database(admin_url, name)
        restore_and_verify(backup_file, rollback_url, manifest_file)
        verify_rollback_database(rollback_url)
    finally:
        drop_database(admin_url, name)
        dropped = True
    return {
        "status": "ok",
        "current_release_sha": current_release_sha,
        "rollback_release_sha": rollback_release_sha,
        "backup_file": str(backup_file),
        "manifest_file": str(manifest_file),
        "rollback_database": name,
        "backup_verified": True,
        "rollback_restore_verified": True,
        "table_counts_verified": True,
        "rollback_database_dropped": dropped,
        "source_database_url_sanitized": sanitize_database_url(database_url),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--current-release-sha", required=True)
    parser.add_argument("--rollback-release-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.database_url, args.workdir, args.current_release_sha, args.rollback_release_sha)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": sanitize_error(str(exc), args.database_url)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
