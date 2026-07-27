#!/usr/bin/env python3
"""Create a portable PostgreSQL custom-format backup and manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


def sanitize_database_url(database_url: str) -> str:
    """Mask credentials in a PostgreSQL URL without logging the original value."""
    parsed = urlsplit(database_url)
    if not parsed.hostname:
        return "<invalid-database-url>"
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    userinfo = parsed.username or ""
    if parsed.password is not None:
        userinfo += ":***"
    netloc = f"{userinfo}@{hostname}" if userinfo else hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def database_name(database_url: str) -> str:
    name = unquote(urlsplit(database_url).path.lstrip("/"))
    if not name:
        raise ValueError("database URL must include a database name")
    return name


def sanitize_text(text: str, database_url: str) -> str:
    sanitized = text.replace(database_url, sanitize_database_url(database_url))
    password = urlsplit(database_url).password
    return sanitized.replace(password, "***") if password else sanitized


def collect_table_counts(database_url: str) -> dict[str, int]:
    import psycopg
    from psycopg import sql

    counts: dict[str, int] = {}
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier("public"), sql.Identifier(table)
                ))
                counts[table] = int(cursor.fetchone()[0])
    return dict(sorted(counts.items()))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], database_url: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required PostgreSQL client executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = sanitize_text(exc.stderr or exc.stdout or "command failed", database_url).strip()
        raise RuntimeError(f"PostgreSQL backup command failed for {sanitize_database_url(database_url)}: {detail}") from exc


def create_backup(
    database_url: str,
    output: Path,
    manifest_path: Path,
    *,
    overwrite: bool = False,
    pg_dump_bin: str = "pg_dump",
) -> dict:
    if output.exists() and not overwrite:
        raise FileExistsError(f"backup file already exists: {output}")
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"manifest file already exists: {manifest_path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    table_counts = collect_table_counts(database_url)
    version_result = _run([pg_dump_bin, "--version"], database_url)
    command = [
        pg_dump_bin, "--format=custom", "--no-owner", "--no-acl",
        "--file", str(output), database_url,
    ]
    _run(command, database_url)
    if not output.is_file():
        raise RuntimeError("pg_dump completed without creating the requested backup file")
    manifest = {
        "backup_file": str(output),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "format": "pg_dump_custom",
        "pg_dump_version": version_result.stdout.strip(),
        "sha256": file_sha256(output),
        "size_bytes": output.stat().st_size,
        "source": {
            "database_name": database_name(database_url),
            "database_url_sanitized": sanitize_database_url(database_url),
        },
        "status": "ok",
        "table_counts": table_counts,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--pg-dump-bin", default=os.environ.get("PG_DUMP_BIN", "pg_dump"))
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.database_url, args.output, args.manifest,
                               overwrite=args.overwrite, pg_dump_bin=args.pg_dump_bin)
    except Exception as exc:
        print(sanitize_text(str(exc), args.database_url), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
