#!/usr/bin/env python3
"""Restore a PostgreSQL custom backup and verify it against its manifest."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postgres_backup import collect_table_counts, file_sha256, sanitize_database_url, sanitize_text


def target_is_empty(database_url: str) -> bool:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public'"
            )
            return int(cursor.fetchone()[0]) == 0


def restore_and_verify(
    backup_file: Path,
    target_database_url: str,
    manifest_path: Path,
    *,
    allow_non_empty_target: bool = False,
    pg_restore_bin: str = "pg_restore",
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_sha256 = file_sha256(backup_file)
    if actual_sha256 != manifest.get("sha256"):
        raise ValueError("backup sha256 does not match manifest")
    if not allow_non_empty_target and not target_is_empty(target_database_url):
        raise ValueError(f"target database is not empty: {sanitize_database_url(target_database_url)}")
    command = [
        pg_restore_bin, "--no-owner", "--no-acl", "--exit-on-error",
        "--dbname", target_database_url, str(backup_file),
    ]
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required PostgreSQL client executable not found: {pg_restore_bin}") from exc
    except subprocess.CalledProcessError as exc:
        detail = sanitize_text(exc.stderr or exc.stdout or "command failed", target_database_url).strip()
        raise RuntimeError(
            f"PostgreSQL restore command failed for {sanitize_database_url(target_database_url)}: {detail}"
        ) from exc
    actual_counts = collect_table_counts(target_database_url)
    expected_counts = {str(key): int(value) for key, value in manifest.get("table_counts", {}).items()}
    if actual_counts != dict(sorted(expected_counts.items())):
        raise ValueError(f"restored table counts do not match manifest for {sanitize_database_url(target_database_url)}")
    return {
        "backup_sha256": actual_sha256,
        "restored_tables_count": len(actual_counts),
        "status": "ok",
        "target_database_url_sanitized": sanitize_database_url(target_database_url),
        "verified_table_counts": actual_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-file", required=True, type=Path)
    parser.add_argument("--target-database-url", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--allow-non-empty-target", action="store_true")
    parser.add_argument("--pg-restore-bin", default=os.environ.get("PG_RESTORE_BIN", "pg_restore"))
    args = parser.parse_args(argv)
    try:
        result = restore_and_verify(
            args.backup_file, args.target_database_url, args.manifest,
            allow_non_empty_target=args.allow_non_empty_target,
            pg_restore_bin=args.pg_restore_bin,
        )
    except Exception as exc:
        print(sanitize_text(str(exc), args.target_database_url), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
