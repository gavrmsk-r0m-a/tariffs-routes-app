#!/usr/bin/env python3
"""Verify PostgreSQL runtime-enablement artifacts without deploying or changing data."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.security import validate_auth_secret  # noqa: E402
from scripts.postgres_backup import file_sha256, sanitize_database_url, sanitize_text  # noqa: E402

APPROVAL_MARKER = "APPROVED_FOR_POSTGRES_RUNTIME_ENABLEMENT"
APPROVAL_PHRASES = (
    "no production credentials are stored in this repository",
    "actual production deployment requires operator/hosting configuration",
)


def _redact(message: str, database_url: str | None, environ: Mapping[str, str]) -> str:
    value = sanitize_text(message, database_url) if database_url else message
    for name in ("MVP_AUTH_SECRET", "SECRET_KEY"):
        secret = environ.get(name, "")
        if secret:
            value = value.replace(secret, "***")
    return value


def check_final_enablement(
    database_url: str | None,
    current_release_sha: str,
    rollback_release_sha: str,
    backup_manifest: Path,
    approval: Path,
    *,
    strict: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict:
    env = dict(os.environ if environ is None else environ)
    current, rollback = current_release_sha.strip(), rollback_release_sha.strip()
    if not current or not rollback:
        raise ValueError("current and rollback release SHAs must be non-empty")
    if current == rollback:
        raise ValueError("current and rollback release SHAs must be different")

    backend = (env.get("DB_BACKEND") or "").strip().lower()
    runtime_guard = (env.get("POSTGRES_RUNTIME_ENABLED") or "").strip()
    effective_url = database_url or env.get("DATABASE_URL")
    if backend not in {"postgres", "postgresql"}:
        raise ValueError("DB_BACKEND must be postgres or postgresql")
    if runtime_guard != "1":
        raise ValueError("POSTGRES_RUNTIME_ENABLED must equal 1")
    if not effective_url:
        raise ValueError("DATABASE_URL is required")
    if (env.get("MVP_PRODUCTION_SECURITY") or "").strip() != "1":
        raise ValueError("MVP_PRODUCTION_SECURITY must equal 1")
    secret_errors = validate_auth_secret(env)
    if secret_errors:
        raise ValueError("invalid production security configuration: " + "; ".join(secret_errors))

    if not approval.is_file():
        raise FileNotFoundError(f"approval artifact does not exist: {approval}")
    approval_text = approval.read_text(encoding="utf-8")
    approval_lower = approval_text.lower()
    if APPROVAL_MARKER not in approval_text:
        raise ValueError("approval marker is missing")
    if not all(phrase in approval_lower for phrase in APPROVAL_PHRASES):
        raise ValueError("approval artifact does not state credential and operator deployment constraints")
    if re.search(r"postgres(?:ql)?://[^\s:/]+:[^@\s]+@", approval_text, re.IGNORECASE):
        raise ValueError("approval artifact must not contain a password-bearing database URL")

    if not backup_manifest.is_file():
        raise FileNotFoundError(f"backup manifest does not exist: {backup_manifest}")
    manifest = json.loads(backup_manifest.read_text(encoding="utf-8"))
    if manifest.get("status") != "ok" or manifest.get("format") != "pg_dump_custom":
        raise ValueError("backup manifest must have status ok and format pg_dump_custom")
    if not isinstance(manifest.get("sha256"), str) or not manifest["sha256"].strip():
        raise ValueError("backup manifest sha256 is required")
    if not isinstance(manifest.get("size_bytes"), int) or manifest["size_bytes"] <= 0:
        raise ValueError("backup manifest size_bytes must be greater than zero")
    if not isinstance(manifest.get("table_counts"), dict) or not manifest["table_counts"]:
        raise ValueError("backup manifest table_counts are required")

    backup_value = manifest.get("backup_file")
    backup_file = Path(backup_value) if isinstance(backup_value, str) and backup_value else None
    if backup_file is not None and not backup_file.is_absolute() and not backup_file.is_file():
        backup_file = backup_manifest.parent / backup_file.name
    warnings: list[str] = []
    digest_verified = False
    if backup_file is not None and backup_file.is_file():
        if file_sha256(backup_file) != manifest["sha256"]:
            raise ValueError("backup sha256 does not match manifest")
        digest_verified = True
    else:
        warnings.append("backup file unavailable; sha256 was not verified")
        if strict:
            raise ValueError(warnings[-1])

    return {
        "status": "ok",
        "final_enablement_ready": not warnings,
        "current_release_sha": current,
        "rollback_release_sha": rollback,
        "required_env_present": True,
        "production_security_valid": True,
        "approval_verified": True,
        "backup_manifest_verified": True,
        "backup_sha256_verified": digest_verified,
        "warnings": warnings,
        "database_url_sanitized": sanitize_database_url(effective_url),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url")
    parser.add_argument("--current-release-sha", required=True)
    parser.add_argument("--rollback-release-sha", required=True)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = check_final_enablement(
            args.database_url, args.current_release_sha, args.rollback_release_sha,
            args.backup_manifest, args.approval, strict=args.strict,
        )
    except Exception as exc:
        message = _redact(str(exc), args.database_url, os.environ)
        result = {"status": "failed", "final_enablement_ready": False, "error": message}
        print(json.dumps(result, sort_keys=True) if args.format == "json" else message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "PostgreSQL final enablement check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
