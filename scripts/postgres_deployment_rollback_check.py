#!/usr/bin/env python3
"""Validate the artifacts required for a PostgreSQL deployment rollback."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postgres_backup import file_sha256, sanitize_database_url, sanitize_text


def sanitize_error(message: str, database_url: str | None) -> str:
    sanitized = sanitize_text(message, database_url) if database_url else message
    auth_secret = os.environ.get("MVP_AUTH_SECRET", "")
    return sanitized.replace(auth_secret, "***") if auth_secret else sanitized


def check_rollback_artifacts(
    current_release_sha: str,
    rollback_release_sha: str,
    backup_manifest: Path,
    *,
    strict: bool = False,
    database_url: str | None = None,
) -> dict:
    """Validate release identifiers, manifest fields, and the optional backup digest."""
    current = current_release_sha.strip()
    rollback = rollback_release_sha.strip()
    if not current or not rollback:
        raise ValueError("current and rollback release SHAs must be non-empty")
    if current == rollback:
        raise ValueError("current and rollback release SHAs must be different")
    if not backup_manifest.is_file():
        raise FileNotFoundError(f"backup manifest does not exist: {backup_manifest}")

    manifest = json.loads(backup_manifest.read_text(encoding="utf-8"))
    if manifest.get("status") != "ok":
        raise ValueError("backup manifest status must be ok")
    if manifest.get("format") != "pg_dump_custom":
        raise ValueError("backup manifest format must be pg_dump_custom")
    if not isinstance(manifest.get("sha256"), str) or not manifest["sha256"].strip():
        raise ValueError("backup manifest sha256 is required")
    if not isinstance(manifest.get("size_bytes"), int) or manifest["size_bytes"] <= 0:
        raise ValueError("backup manifest size_bytes must be greater than zero")
    if not isinstance(manifest.get("table_counts"), dict) or not manifest["table_counts"]:
        raise ValueError("backup manifest table_counts are required")

    warnings: list[str] = []
    backup_value = manifest.get("backup_file")
    backup_file = Path(backup_value) if isinstance(backup_value, str) and backup_value else None
    if backup_file is not None and not backup_file.is_absolute() and not backup_file.is_file():
        backup_file = backup_manifest.parent / backup_file.name
    digest_verified = False
    if backup_file is not None and backup_file.is_file():
        if file_sha256(backup_file) != manifest["sha256"]:
            raise ValueError("backup sha256 does not match manifest")
        digest_verified = True
    else:
        warnings.append("backup file unavailable; sha256 was not verified")
        if strict:
            raise ValueError(warnings[-1])

    result = {
        "status": "ok",
        "current_release_sha": current,
        "rollback_release_sha": rollback,
        "manifest_verified": True,
        "backup_sha256_verified": digest_verified,
        "table_counts_present": True,
        "warnings": warnings,
    }
    if database_url is not None:
        result["database_url_sanitized"] = sanitize_database_url(database_url)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-release-sha", required=True)
    parser.add_argument("--rollback-release-sha", required=True)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check_rollback_artifacts(
            args.current_release_sha, args.rollback_release_sha, args.backup_manifest,
            strict=args.strict, database_url=args.database_url,
        )
    except Exception as exc:
        message = sanitize_error(str(exc), args.database_url)
        result = {"status": "failed", "error": message}
        print(json.dumps(result, sort_keys=True) if args.format == "json" else message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "deployment rollback check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
