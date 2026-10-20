#!/usr/bin/env python3
"""Create or deliberately rotate an Archivarius service token."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.archivarius import token_hash
from app.db import connect_postgres

MIN_TOKEN_LENGTH = 32


def _validate_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise ValueError("token is empty")
    if len(value) < MIN_TOKEN_LENGTH:
        raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
    return value


def provision(
    database_url: str,
    name: str,
    created_by_user: str,
    *,
    rotate: bool,
    comment: str | None = None,
    token: str | None = None,
) -> str:
    token_value = _validate_token(token) if token is not None else secrets.token_urlsafe(48)
    digest = token_hash(token_value)
    conn = connect_postgres(database_url)
    try:
        user = conn.execute("SELECT id FROM users WHERE username=%s AND is_active IS TRUE", (created_by_user,)).fetchone()
        if user is None:
            raise ValueError("created-by user does not exist or is inactive")
        existing = conn.execute("SELECT id FROM api_tokens WHERE name=%s", (name,)).fetchone()
        if existing and not rotate:
            raise ValueError("token name already exists; pass --rotate to replace it deliberately")
        if existing:
            conn.execute(
                """UPDATE api_tokens SET token_hash=%s, is_active=TRUE, created_by=%s,
                         created_at=CURRENT_TIMESTAMP, last_used_at=NULL, comment=%s WHERE id=%s""",
                (digest, user["id"], comment, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO api_tokens(name,token_hash,is_active,created_by,comment) VALUES (%s,%s,TRUE,%s,%s)",
                (name, digest, user["id"], comment),
            )
        conn.commit()
        return token_value
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _supplied_token(args: argparse.Namespace) -> str | None:
    if args.token_stdin:
        return _validate_token(sys.stdin.read())
    if args.token_env_var:
        raw = os.environ.get(args.token_env_var)
        if raw is None:
            raise ValueError(f"environment variable {args.token_env_var} is not set")
        return _validate_token(raw)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--created-by-user", required=True)
    parser.add_argument("--comment")
    parser.add_argument("--rotate", action="store_true", help="replace an existing named token")
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument(
        "--token-stdin",
        action="store_true",
        help="read an existing plaintext token from stdin; the token will not be printed",
    )
    token_source.add_argument(
        "--token-env-var",
        help="read an existing plaintext token from this environment variable; the token will not be printed",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")

    try:
        supplied = _supplied_token(args)
        token = provision(
            args.database_url,
            args.name,
            args.created_by_user,
            rotate=args.rotate,
            comment=args.comment,
            token=supplied,
        )
    except Exception as exc:
        print(f"Token was not created: {exc}", file=sys.stderr)
        return 1

    if supplied is not None:
        print("Archivarius token stored successfully. The plaintext token was not printed.")
    else:
        print("Store this token securely. It will not be shown again:")
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
